"""TD-regression flag: is a projected touchdown count out of line with its own yardage?

Touchdowns are the noisiest line in any projection and the one with the most fantasy points
riding on it. A projection that gives a receiver 1,000 yards and 12 scores is not obviously
wrong player-by-player, but it is a long way from what 1,000-yard receivers actually do. This
module fits that relationship on cached history and flags the outliers.

**A flag, not an adjustment.** Nothing here changes a projected number. Read
:func:`fit_td_models`' reported ``r2`` before deciding this earns more than a badge -- for
receiving touchdowns it is around 0.3-0.55, i.e. yardage explains at best half of who scores.

How the fit works, and why it is shaped this way:

* **Through-origin rate model**, ``td = slope * predictor``. A player with no yards scores no
  touchdowns, so an intercept has no meaning here, and forcing the line through the origin
  makes the model equally valid for a 6-game season and a 17-game one -- which matters, because
  the historical sample is full of injured seasons and the projections are not.
* **The slope is the POOLED rate** (total touchdowns / total predictor over the fitted sample),
  not ordinary least squares. This is not a style preference, it is forced by the variance model
  two bullets down: if ``var(td) = dispersion * slope * x``, the maximum-likelihood through-origin
  slope is exactly ``sum(y)/sum(x)``, while OLS assumes constant variance and so over-weights
  the highest-volume players -- who score at a higher rate than everyone else. Measured on the
  2025 actuals, OLS ran 1.7% hot on QB passing TDs, 8.7% on RB receiving TDs and **15.7% on QB
  rushing TDs**, which was enough to make every one of the three sources look like it was
  under-projecting quarterback rushing touchdowns by 20-30% when it was not. ``ols_slope`` is
  kept on every model so that comparison stays reproducible.
* **The predictor is chosen by the data**, not by preference: every candidate in
  :data:`CANDIDATE_PREDICTORS` is fitted and the highest-R2 one wins. Yardage wins almost
  everywhere; QB rushing touchdowns are the exception.
* **The usage floor is data-defined too** -- the median of the non-zero values of that
  predictor in the historical sample. Fitting on everybody piles hundreds of near-origin
  points onto the line, which inflates R2 without telling you anything about the players a
  draft board actually ranks.
* **Counts are overdispersed, not normal.** Residual variance is modelled as
  ``dispersion * expected``, the Poisson shape, with ``dispersion`` measured from the residuals
  rather than assumed to be 1.0.
* **The flag threshold is a fitted quantile of |z| in the historical sample**, so "outlier"
  means "further from its own yardage than N% of real player-seasons were". No threshold in
  this file was chosen because it looked round.

**The limits, stated up front.** The only cached history with touchdowns in it is ONE season
(the ESPN payload's 2025 weekly actuals; the seven-season ``nflreadpy_weekly`` cache carries
passing/rushing/receiving YARDS and nothing else). So every rate here is 2025's league rate,
fitted on 48-120 player-seasons per position group, and the year-to-year stability of those
rates is **not measurable from anything cached offline**. Treat the rates as one season's, and
expect the flag to move if a second season ever lands.

One more asymmetry worth knowing before reading a z-score: the historical spread being measured
is *realised* variance -- real players' actual luck -- while a projection is supposed to be an
expectation, which should be LESS dispersed than reality. Judging a projection against the
dispersion of outcomes is therefore a conservative test: it under-flags rather than over-flags,
and anything it does flag is genuinely aggressive.

Reads only cached files under ``data/raw/``. No network call on any path here.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Callable, Iterable, Mapping, Sequence

from draftroom.prep.schema import StatLine

log = logging.getLogger("draftroom.valuation.td_regression")

__all__ = [
    "CANDIDATE_PREDICTORS",
    "CANDIDATE_PREDICTORS_WITH_TARGETS",
    "TD_MODEL_CAVEAT",
    "PlayerActual",
    "TdModel",
    "TdModelSet",
    "TdFlag",
    "SourceBias",
    "source_bias",
    "RateCalibration",
    "backtest_rate_calibration",
    "player_season_actuals",
    "fit_td_models",
    "fit_one_model",
    "flag_statlines",
]

#: ``(pos, td_stat) -> candidate predictor stats``. Every candidate is fitted; the one with the
#: highest through-origin R2 is kept. Receiving/rushing TDs for positions that barely record
#: them (a WR's rush_td, a TE's rush_td) are deliberately absent -- a rate fitted on a handful
#: of gadget carries would be noise wearing a coefficient's clothes.
#:
#: ``rec_tgt`` IS ABSENT ON PURPOSE, and this is the most consequential choice in the file.
#: Targets exist in only ONE of the three source families (ESPN; Sleeper carries no target
#: field at all and FantasyPros publishes no targets column -- see CLAUDE.md), so a model
#: predicting off targets can be applied to a third of the board and silently skips the rest.
#: When rec_tgt was allowed as a candidate it won on R2 for all three receiving groups and the
#: flag went completely inert on Sleeper and FantasyPros. It won by almost nothing: measured on
#: the 2025 actuals, WR rec_td R2 was rec_tgt 0.542 / rec_yd 0.536 / rec 0.525, RB rec_td 0.541
#: / 0.527 / 0.519, TE rec_td 0.405 / 0.377 / 0.397. Trading two thirds of the board's coverage
#: for 0.006 of R2 is not a trade. See :data:`CANDIDATE_PREDICTORS_WITH_TARGETS` to reproduce it.
CANDIDATE_PREDICTORS: Mapping[tuple[str, str], tuple[str, ...]] = {
    ("QB", "pass_td"): ("pass_yd", "pass_att", "pass_cmp"),
    ("QB", "rush_td"): ("rush_yd", "rush_att"),
    ("RB", "rush_td"): ("rush_yd", "rush_att"),
    ("RB", "rec_td"): ("rec_yd", "rec"),
    ("WR", "rec_td"): ("rec_yd", "rec"),
    ("TE", "rec_td"): ("rec_yd", "rec"),
}

#: The same candidates with ``rec_tgt`` added back, kept only so the choice above is
#: reproducible. Fitting with this dict produces better R2 and a flag that cannot see two of
#: the three source families.
CANDIDATE_PREDICTORS_WITH_TARGETS: Mapping[tuple[str, str], tuple[str, ...]] = {
    **CANDIDATE_PREDICTORS,
    ("RB", "rec_td"): ("rec_yd", "rec", "rec_tgt"),
    ("WR", "rec_td"): ("rec_yd", "rec", "rec_tgt"),
    ("TE", "rec_td"): ("rec_yd", "rec", "rec_tgt"),
}

TD_MODEL_CAVEAT = (
    "Fitted on ONE season (2025) of cached ESPN weekly actuals -- the only cached history that "
    "carries touchdowns at all; the seven-season nflreadpy cache has yardage only. Per-group "
    "samples are 48-120 player-seasons and the year-to-year stability of these rates cannot be "
    "checked offline. R2 is ~0.9 for QB passing touchdowns but only ~0.3-0.55 for receiving "
    "touchdowns, so for receivers this flag says 'unusual given the yardage', which is a long "
    "way from 'wrong'. It is a flag, never an adjustment, and it is a deliberately conservative "
    "one: it measures a projected expectation against the dispersion of realised outcomes, "
    "which under-flags by construction."
)


@dataclass(frozen=True)
class PlayerActual:
    """One player's real season, aggregated from cached weekly actuals."""

    player_id: str
    name: str
    pos: str
    games: int
    stats: Mapping[str, float]

    def get(self, stat: str) -> float:
        return float(self.stats.get(stat, 0.0))


@dataclass(frozen=True)
class TdModel:
    """One fitted ``td_stat = slope * predictor`` relationship, with its whole provenance."""

    pos: str
    td_stat: str
    predictor: str
    #: Pooled (Poisson-MLE) through-origin rate: total TDs / total predictor. See
    #: :func:`_fit_through_origin` for why this and not OLS.
    slope: float
    #: The OLS through-origin slope on the same sample, for the record only. Never used to
    #: predict; kept so the "OLS runs hot" finding stays reproducible.
    ols_slope: float
    r2: float
    resid_sd: float
    #: Residual variance divided by mean expectation. 1.0 would be exactly Poisson.
    dispersion: float
    n: int
    #: Only players at or above this predictor value were fitted; see ``usage_floor_rule``.
    usage_floor: float
    usage_floor_rule: str
    seasons: tuple[int, ...]
    #: Empirical |z| quantiles in the fitted sample: the flag threshold comes from here.
    z_quantiles: Mapping[float, float]
    #: R2 of every candidate predictor tried, so the choice is auditable.
    candidate_r2: Mapping[str, float]

    def expected(self, predictor_value: float) -> float:
        return self.slope * float(predictor_value)

    def z(self, td_value: float, predictor_value: float) -> tuple[float, float]:
        """``(z, expected)``. z is signed: positive means MORE touchdowns than the yardage buys."""
        exp = self.expected(predictor_value)
        sd = math.sqrt(max(self.dispersion * exp, 0.0))
        if sd <= 0.0:
            return 0.0, exp
        return (float(td_value) - exp) / sd, exp

    def threshold(self, quantile: float) -> float:
        if quantile in self.z_quantiles:
            return self.z_quantiles[quantile]
        keys = sorted(self.z_quantiles)
        if not keys:
            raise ValueError(f"{self.pos}/{self.td_stat}: no fitted z quantiles to threshold on")
        nearest = min(keys, key=lambda k: abs(k - quantile))
        log.warning(
            "no fitted z quantile at %.2f for %s/%s; using the nearest fitted one (%.2f)",
            quantile, self.pos, self.td_stat, nearest,
        )
        return self.z_quantiles[nearest]


@dataclass(frozen=True)
class TdModelSet:
    models: Mapping[tuple[str, str], TdModel]
    provenance: Mapping[str, object]
    caveat: str = TD_MODEL_CAVEAT


@dataclass(frozen=True)
class TdFlag:
    """One projected touchdown figure that sits outside the fitted historical dispersion."""

    player_id: str
    name: str
    pos: str
    td_stat: str
    projected_td: float
    predictor: str
    predictor_value: float
    expected_td: float
    z: float
    threshold: float
    model: TdModel

    @property
    def direction(self) -> str:
        return "high" if self.z > 0 else "low"

    @property
    def delta(self) -> float:
        return self.projected_td - self.expected_td


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------


def player_season_actuals(
    espn_raw: Mapping[str, object], season: int
) -> dict[str, PlayerActual]:
    """Per-player real season totals for ``season``, from the cached ESPN weekly ACTUALS.

    Aggregating the weekly blocks (``statSourceId == 0``, ``statSplitTypeId == 1``) rather than
    reading the single season-total actual block gets ~640 players instead of ~355, and gives a
    real games-played count for free -- which is what lets a 6-game season stay in the sample
    without distorting a through-origin rate.
    """
    from draftroom.prep import espn_client as espn

    players = espn_raw.get("players") if isinstance(espn_raw, Mapping) else None
    if not isinstance(players, list):
        raise ValueError(
            "ESPN payload has no 'players' list; cannot fit TD rates from it. Do not guess a "
            "fix -- inspect the cached file."
        )

    out: dict[str, PlayerActual] = {}
    for entry in players:
        player = (entry or {}).get("player") or {}
        pid = player.get("id")
        pos = espn.ESPN_POSITION_MAP.get(player.get("defaultPositionId"))
        if pid is None or pos not in espn.SKILL_POSITIONS:
            continue
        stats: dict[str, float] = {}
        games = 0
        for block in player.get("stats") or []:
            if (
                block.get("seasonId") != season
                or block.get("statSourceId") != 0
                or block.get("statSplitTypeId") != 1
            ):
                continue
            raw_stats = block.get("stats") or {}
            if not raw_stats:
                continue
            games += 1
            for raw_key, value in raw_stats.items():
                try:
                    stat_id = int(raw_key)
                except (TypeError, ValueError):
                    continue
                canonical = espn.ESPN_STAT_ID_MAP.get(stat_id)
                if canonical and canonical != "games":
                    stats[canonical] = stats.get(canonical, 0.0) + float(value or 0.0)
        if games:
            out[str(pid)] = PlayerActual(
                player_id=str(pid),
                name=player.get("fullName") or str(pid),
                pos=pos,
                games=games,
                stats=stats,
            )
    return out


# ---------------------------------------------------------------------------
# Fitting
# ---------------------------------------------------------------------------


def _median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    if not n:
        return 0.0
    return ordered[n // 2] if n % 2 else (ordered[n // 2 - 1] + ordered[n // 2]) / 2.0


def _quantile(ordered: Sequence[float], q: float) -> float:
    if not ordered:
        return float("nan")
    idx = q * (len(ordered) - 1)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    frac = idx - lo
    return ordered[lo] * (1.0 - frac) + ordered[hi] * frac


def _r2_through_origin(xs: Sequence[float], ys: Sequence[float], slope: float) -> float:
    """R2 of ``y = slope*x`` against the mean of y, so it is comparable across candidates."""
    mean_y = sum(ys) / len(ys) if ys else 0.0
    ss_res = sum((y - slope * x) ** 2 for x, y in zip(xs, ys))
    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    return (1.0 - ss_res / ss_tot) if ss_tot else float("nan")


def _fit_through_origin(
    xs: Sequence[float], ys: Sequence[float]
) -> tuple[float, float, float]:
    """``(pooled_slope, ols_slope, r2_of_pooled)`` for ``y = b*x``.

    ``pooled_slope = sum(y)/sum(x)`` is the maximum-likelihood through-origin slope when the
    variance is proportional to the mean (the Poisson shape this module assumes), and it is the
    one used everywhere. ``ols_slope = sum(xy)/sum(x^2)`` is returned only so the difference
    stays visible -- it assumes constant variance, which counts data do not have, and it runs
    systematically hot because the biggest-volume players also score at the highest rate.
    """
    sx = sum(xs)
    sxx = sum(x * x for x in xs)
    pooled = (sum(ys) / sx) if sx else 0.0
    ols = (sum(x * y for x, y in zip(xs, ys)) / sxx) if sxx else 0.0
    return pooled, ols, _r2_through_origin(xs, ys, pooled)


#: |z| quantiles computed for every model. 0.95 is the default flag threshold; the others are
#: reported so a reader can see how quickly the tail steepens.
Z_QUANTILES: tuple[float, ...] = (0.50, 0.75, 0.90, 0.95, 0.99)

#: Below this many fitted player-seasons a model is not produced at all. Set to the smallest
#: group this repo's cached history actually supports (QB rushing TDs, n=32) so nothing is
#: silently fitted on a handful of rows.
MIN_FIT_ROWS = 30


def fit_one_model(
    pos: str,
    td_stat: str,
    actuals: Iterable[PlayerActual],
    *,
    candidates: Sequence[str],
    z_quantiles: Sequence[float] = Z_QUANTILES,
) -> TdModel | None:
    """Fit one ``(pos, td_stat)`` model, choosing the predictor with the highest R2."""
    rows = [a for a in actuals if a.pos == pos]
    if not rows:
        return None

    best: TdModel | None = None
    candidate_r2: dict[str, float] = {}
    for predictor in candidates:
        nonzero = [a.get(predictor) for a in rows if a.get(predictor) > 0]
        if len(nonzero) < MIN_FIT_ROWS:
            continue
        floor = _median(nonzero)
        sample = [a for a in rows if a.get(predictor) >= floor and a.get(predictor) > 0]
        if len(sample) < MIN_FIT_ROWS:
            continue
        xs = [a.get(predictor) for a in sample]
        ys = [a.get(td_stat) for a in sample]
        slope, ols_slope, r2 = _fit_through_origin(xs, ys)
        candidate_r2[predictor] = r2
        if slope <= 0:
            continue

        residuals = [y - slope * x for x, y in zip(xs, ys)]
        mean_expected = sum(slope * x for x in xs) / len(xs)
        dispersion = (
            (sum(r * r for r in residuals) / len(residuals)) / mean_expected
            if mean_expected > 0
            else 0.0
        )
        resid_sd = math.sqrt(sum(r * r for r in residuals) / max(len(residuals) - 1, 1))
        zs = sorted(
            abs(r) / math.sqrt(dispersion * slope * x)
            for r, x in zip(residuals, xs)
            if dispersion > 0 and slope * x > 0
        )
        model = TdModel(
            pos=pos,
            td_stat=td_stat,
            predictor=predictor,
            slope=slope,
            ols_slope=ols_slope,
            r2=r2,
            resid_sd=resid_sd,
            dispersion=dispersion,
            n=len(sample),
            usage_floor=floor,
            usage_floor_rule=(
                f"median of non-zero {predictor} among {pos}s in the fitted seasons "
                f"({len(nonzero)} players)"
            ),
            seasons=(),
            z_quantiles={q: _quantile(zs, q) for q in z_quantiles},
            candidate_r2={},
        )
        if best is None or (r2 == r2 and r2 > best.r2):  # r2==r2 rejects NaN
            best = model

    if best is None:
        return None
    from dataclasses import replace

    return replace(best, candidate_r2=dict(candidate_r2))


def fit_td_models(
    actuals: Mapping[str, PlayerActual] | Iterable[PlayerActual],
    *,
    seasons: Sequence[int],
    candidates: Mapping[tuple[str, str], tuple[str, ...]] = CANDIDATE_PREDICTORS,
    z_quantiles: Sequence[float] = Z_QUANTILES,
) -> TdModelSet:
    """Fit every ``(pos, td_stat)`` model in ``candidates``."""
    rows = list(actuals.values()) if isinstance(actuals, Mapping) else list(actuals)
    from dataclasses import replace

    models: dict[tuple[str, str], TdModel] = {}
    skipped: list[str] = []
    for (pos, td_stat), predictors in candidates.items():
        model = fit_one_model(
            pos, td_stat, rows, candidates=predictors, z_quantiles=z_quantiles
        )
        if model is None:
            skipped.append(f"{pos}/{td_stat}")
            continue
        models[(pos, td_stat)] = replace(model, seasons=tuple(seasons))

    return TdModelSet(
        models=models,
        provenance={
            "seasons": tuple(seasons),
            "n_player_seasons_available": len(rows),
            "source": "ESPN cached weekly actuals, statSourceId=0/statSplitTypeId=1",
            "min_fit_rows": MIN_FIT_ROWS,
            "skipped": tuple(skipped),
            "limits": (
                "One season only. No cached history carries touchdowns for any other year, so "
                "year-to-year stability of these rates is unmeasurable offline."
            ),
        },
    )


# ---------------------------------------------------------------------------
# Flagging projections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceBias:
    """One source's AGGREGATE touchdown level for one ``(pos, td_stat)`` group.

    Far more informative than the per-player flag, and cheaper to trust. A per-player z-score
    asks a question the data can barely answer (R2 ~0.5 on receiving TDs). Summed over a
    hundred players the noise cancels and what is left is the source's *rate*: whether it hands
    out more or fewer touchdowns per yard than the league actually produced. A source-wide bias
    is also the only shape of finding that a per-source, per-stat rejection rule could act on.
    """

    source: str
    pos: str
    td_stat: str
    n_players: int
    projected_total: float
    expected_total: float
    #: projected / expected. 1.0 == exactly the fitted historical rate.
    ratio: float
    #: Aggregate z, using the fitted overdispersion: (proj - exp) / sqrt(dispersion * exp).
    z: float
    model: TdModel


def source_bias(
    source: str,
    statlines: Mapping[str, StatLine],
    pos_of: Callable[[str], str] | Mapping[str, str],
    modelset: TdModelSet,
) -> tuple[SourceBias, ...]:
    """Aggregate projected vs fitted-expected touchdowns, per position group, for one source."""
    pf = pos_of.get if isinstance(pos_of, Mapping) else pos_of

    out: list[SourceBias] = []
    for (pos, td_stat), model in sorted(modelset.models.items()):
        n = 0
        proj = 0.0
        exp = 0.0
        for pid, line in statlines.items():
            if ((pf(pid) or "").strip().upper() if pf else "") != pos:
                continue
            predictor_value = float(getattr(line, model.predictor, 0.0) or 0.0)
            if predictor_value < model.usage_floor:
                continue
            n += 1
            proj += float(getattr(line, td_stat, 0.0) or 0.0)
            exp += model.expected(predictor_value)
        if not n or exp <= 0:
            continue
        var = model.dispersion * exp
        out.append(
            SourceBias(
                source=source,
                pos=pos,
                td_stat=td_stat,
                n_players=n,
                projected_total=proj,
                expected_total=exp,
                ratio=proj / exp,
                z=(proj - exp) / math.sqrt(var) if var > 0 else 0.0,
                model=model,
            )
        )
    return tuple(out)


@dataclass(frozen=True)
class RateCalibration:
    """One source's PROJECTED touchdown rate against the ACTUAL rate, for a season now known.

    The only true calibration check available from THIS REPO'S CACHE, and it exists for exactly
    one source. The cached ESPN payload carries both its own season projection AND the realised
    actuals for 2025, keyed by the same player id, so ESPN's projected TD-per-yard rate can be
    compared to the rate that actually happened. Nothing cached under
    ``data/raw/sleeper_projections/`` is 2025, and the FantasyPros CSVs are 2026 exports, so a
    finding here says something about ESPN and NOTHING comparative about the other two.

    Extending it is a prep-phase fetch, not a modelling problem: the 2025 backtest (retired 2026-08-25; conclusions in ``docs/archive/SOURCE_BACKTEST.md``)
    (and ``docs/archive/SOURCE_BACKTEST.md``) establishes that Sleeper's 2025 preseason projections ARE
    retrievable, 3,115 records, and verified preseason by content. Cache those and this function
    covers two of the three families. FantasyPros stays unmeasurable -- its historical download
    sits behind the subscription CLAUDE.md says not to buy.

    Rates, not totals, are the comparison: projected TOTALS overshoot every year simply because
    projections do not know who will get hurt, so a total-vs-total ratio measures availability
    optimism, not touchdown-rate calibration.
    """

    source: str
    pos: str
    td_stat: str
    predictor: str
    season: int
    n_players: int
    projected_td: float
    actual_td: float
    projected_predictor: float
    actual_predictor: float
    #: TDs per unit of predictor, projected and actual.
    projected_rate: float
    actual_rate: float

    @property
    def rate_ratio(self) -> float:
        return self.projected_rate / self.actual_rate if self.actual_rate else float("nan")


def backtest_rate_calibration(
    source: str,
    projections: Mapping[str, StatLine],
    actuals: Mapping[str, PlayerActual],
    modelset: TdModelSet,
    *,
    season: int,
) -> tuple[RateCalibration, ...]:
    """Projected vs actual TD RATE for ``season``, on the players present in both.

    Eligibility uses the PROJECTED predictor value against the model's usage floor, which is
    what a draft-time check has available -- selecting on the actual instead would condition on
    the outcome and quietly flatter the projection.
    """
    out: list[RateCalibration] = []
    for (pos, td_stat), model in sorted(modelset.models.items()):
        proj_td = proj_x = act_td = act_x = 0.0
        n = 0
        for pid, actual in actuals.items():
            if actual.pos != pos:
                continue
            projected = projections.get(pid)
            if projected is None:
                continue
            pv = float(getattr(projected, model.predictor, 0.0) or 0.0)
            if pv < model.usage_floor:
                continue
            n += 1
            proj_x += pv
            proj_td += float(getattr(projected, td_stat, 0.0) or 0.0)
            act_x += actual.get(model.predictor)
            act_td += actual.get(td_stat)
        if not n or proj_x <= 0 or act_x <= 0:
            continue
        out.append(
            RateCalibration(
                source=source,
                pos=pos,
                td_stat=td_stat,
                predictor=model.predictor,
                season=season,
                n_players=n,
                projected_td=proj_td,
                actual_td=act_td,
                projected_predictor=proj_x,
                actual_predictor=act_x,
                projected_rate=proj_td / proj_x,
                actual_rate=act_td / act_x,
            )
        )
    return tuple(out)


def flag_statlines(
    statlines: Mapping[str, StatLine],
    pos_of: Callable[[str], str] | Mapping[str, str],
    modelset: TdModelSet,
    *,
    name_of: Callable[[str], str] | Mapping[str, str] | None = None,
    quantile: float = 0.95,
) -> tuple[TdFlag, ...]:
    """Flag every projected touchdown figure whose |z| exceeds the fitted historical quantile.

    A player is skipped, never flagged, when his predictor value is below the model's usage
    floor: the model was not fitted on players that small, so it has nothing to say about them.
    """
    pf = pos_of.get if isinstance(pos_of, Mapping) else pos_of
    nf = (name_of.get if isinstance(name_of, Mapping) else name_of) if name_of else None

    out: list[TdFlag] = []
    for pid, line in statlines.items():
        pos = (pf(pid) or "").strip().upper() if pf else ""
        if not pos:
            continue
        for (model_pos, td_stat), model in modelset.models.items():
            if model_pos != pos:
                continue
            predictor_value = float(getattr(line, model.predictor, 0.0) or 0.0)
            if predictor_value < model.usage_floor:
                continue
            td_value = float(getattr(line, td_stat, 0.0) or 0.0)
            z, expected = model.z(td_value, predictor_value)
            threshold = model.threshold(quantile)
            if not (threshold == threshold) or abs(z) <= threshold:
                continue
            out.append(
                TdFlag(
                    player_id=pid,
                    name=((nf(pid) if nf else None) or pid),
                    pos=pos,
                    td_stat=td_stat,
                    projected_td=td_value,
                    predictor=model.predictor,
                    predictor_value=predictor_value,
                    expected_td=expected,
                    z=z,
                    threshold=threshold,
                    model=model,
                )
            )
    out.sort(key=lambda f: -abs(f.z))
    return tuple(out)
