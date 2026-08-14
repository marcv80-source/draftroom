"""Survival: the probability a player is still on the board when I pick again.

Three ideas, in order of how much they matter.

**1. Conditioning.** ADP says *where* a player usually goes; it says nothing about the fact
that he is demonstrably still sitting there right now. The unconditional survival ``S(N)``
answers "does he last to pick N", but at pick 20 with the guy still available that question
has already been half-answered. The right quantity is ``S(N) / S(n0)`` -- survival to N
*given* survival to now. Unconditioned numbers are always too pessimistic and they get worse
the longer a player slides, which is exactly the situation where the answer matters. This is
the step most public tools get wrong (CLAUDE.md, "Survival conditioned on the player still
being on the board").

**2. A logistic, not a normal.** FFC publishes a mean ADP and a standard deviation, not a
distribution. The logistic is the natural choice: it is the CDF of "how much longer until
somebody takes him", it has fatter tails than the Gaussian (draft-day surprises are real),
and it matches a standard deviation through ``s = sd * sqrt(3)/pi``. Written as
``S(N) = 1/(1 + exp((N-mu)/s))`` it is also numerically clean -- no cancellation near 1.

**3. Runs are real and ADP does not know about them.** When four quarterbacks go in six
picks, every remaining quarterback's true ADP has moved forward and the published mean is
stale. :class:`PositionalRun` detects that, but it normalizes against the composition of the
board: in a 2QB league a QB-dense remaining pool means QB picks are *expected*, and a
detector that just counts "3 of the last 5" would fire permanently and be useless.
"""

from __future__ import annotations

import glob
import json
import math
import os
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from draftroom.config import REPO_ROOT

__all__ = [
    "AdpPlayer",
    "SdFit",
    "PositionalRun",
    "RunReading",
    "LOGISTIC_SCALE_FACTOR",
    "MIN_SD",
    "logistic_scale",
    "p_drafted_by",
    "survival",
    "p_available",
    "fit_sd_model",
    "load_ffc_adp",
    "expected_survivors",
    "survival_curve",
    "tier_exhaustion_pick",
]

#: A logistic with scale ``s`` has standard deviation ``s * pi / sqrt(3)``. Invert it.
LOGISTIC_SCALE_FACTOR = math.sqrt(3.0) / math.pi

#: UNVERIFIED TUNING CONSTANT. Floor on the standard deviation, in picks. FFC's smallest
#: observed stdev in the cached 2026 payload is 0.6, so this never binds on real data; it
#: exists so a hand-built or zero-variance player cannot divide by zero. A quarter of a pick
#: is narrow enough to behave like a near-certainty.
MIN_SD = 0.25

#: Exponent past which ``exp`` overflows a float64. Beyond it the survival is 0 to ~300
#: decimal places, so short-circuiting loses nothing.
_MAX_EXP = 700.0


# --------------------------------------------------------------------------- player shape


@dataclass(frozen=True)
class AdpPlayer:
    """One row of ADP, reduced to what the survival model needs.

    ``stdev`` is optional: ``None`` means "use the fitted sd-vs-ADP relationship", which is
    what :func:`fit_sd_model` exists for.
    """

    player_id: str
    name: str
    pos: str
    adp: float
    stdev: float | None = None
    team: str = ""
    bye: int | None = None


def _mu_sd(player: Any) -> tuple[float, float | None]:
    """Pull (mean ADP, stdev) out of anything player-ish.

    Accepts :class:`AdpPlayer`, :class:`~draftroom.prep.ffc_client.AdpRow` (which spells the
    field ``std_dev``), plain mappings, and bare ``(mu, sd)`` pairs.
    """
    if isinstance(player, (tuple, list)) and len(player) == 2:
        mu, sd = player
        return float(mu), (None if sd is None else float(sd))

    if isinstance(player, Mapping):
        get = player.get
    else:
        def get(key: str, default: Any = None) -> Any:
            return getattr(player, key, default)

    mu = None
    for key in ("adp", "mu", "adp_mean"):
        value = get(key)
        if value is not None:
            mu = float(value)
            break
    if mu is None:
        raise KeyError(f"no ADP field on {player!r} (looked for adp/mu/adp_mean)")

    for key in ("stdev", "std_dev", "sd", "sigma"):
        value = get(key)
        if value is not None:
            return mu, float(value)
    return mu, None


def _pos_of(player: Any) -> str:
    if isinstance(player, Mapping):
        raw = player.get("pos") or player.get("position") or ""
    else:
        raw = getattr(player, "pos", None) or getattr(player, "position", "") or ""
    return str(raw).upper()


# ------------------------------------------------------------------------------- logistic


def logistic_scale(sd: float) -> float:
    """Logistic scale parameter ``s`` for a distribution with standard deviation ``sd``."""
    return max(float(sd), MIN_SD) * LOGISTIC_SCALE_FACTOR


def p_drafted_by(mu: float, sd: float, pick: float) -> float:
    """``F(N)``: probability the player is gone by pick ``N``."""
    return 1.0 - survival(mu, sd, pick)


def survival(mu: float, sd: float, pick: float) -> float:
    """``S(N) = 1 - F(N)``: unconditional probability the player lasts to pick ``N``.

    Written as ``1/(1+exp(z))`` with ``z = (N-mu)/s`` rather than ``1 - 1/(1+exp(-z))``,
    which loses all precision in the tail that matters most (a stud lasting to a late pick).
    """
    s = logistic_scale(sd)
    z = (float(pick) - float(mu)) / s
    if z >= _MAX_EXP:
        return 0.0
    if z <= -_MAX_EXP:
        return 1.0
    return 1.0 / (1.0 + math.exp(z))


def p_available(
    mu: float,
    sd: float | None,
    target_pick: float,
    current_pick: float,
    *,
    fit: "SdFit | None" = None,
) -> float:
    """Probability the player survives to ``target_pick`` **given he is available now**.

    ``P = S(target) / S(current)``. The conditioning is the whole point: the player is on the
    board at ``current_pick``, which is information the raw ADP curve does not have. It is
    never smaller than the unconditional ``S(target)`` and the gap widens the further a
    player has already slid.

    Args:
        mu: mean ADP.
        sd: ADP standard deviation, or ``None`` to fall back to ``fit``.
        target_pick: the pick we are asking about (usually my next turn).
        current_pick: the pick on the clock right now.
        fit: fitted sd-vs-ADP relationship, used only when ``sd`` is ``None``.

    Returns:
        A probability in ``[0, 1]``. Exactly ``1.0`` when ``target_pick <= current_pick``
        (he is available now, so "available now or sooner" is certain).
    """
    if sd is None:
        if fit is None:
            raise ValueError(
                f"player at ADP {mu} has no stdev and no SdFit fallback was supplied; "
                "pass fit=fit_sd_model(...) rather than inventing a spread"
            )
        sd = fit.predict(mu)

    if target_pick <= current_pick:
        return 1.0

    s_current = survival(mu, sd, current_pick)
    if s_current <= 0.0:
        # He "cannot" be here, yet he is -- the model has already been falsified for this
        # player, so conditioning on it is meaningless. Treat the far tail as certain-gone
        # rather than dividing by zero and reporting a wild number.
        return 0.0
    return min(1.0, survival(mu, sd, target_pick) / s_current)


# -------------------------------------------------------------------- empirical sd widening


@dataclass(frozen=True)
class SdFit:
    """Least-squares fit of ADP standard deviation against mean ADP: ``sd = a + b*mu``.

    Uncertainty widens as you go down the board -- everyone agrees on the first pick and
    nobody agrees on the 150th. This is the fallback spread for a player who has an ADP but
    no published stdev (a late crosswalk addition, a hand-entered sleeper).
    """

    intercept: float
    slope: float
    n: int
    r2: float
    #: sd never goes below this, whatever the line says at small mu.
    floor: float = MIN_SD

    def predict(self, mu: float) -> float:
        return max(self.floor, self.intercept + self.slope * float(mu))

    def describe(self) -> str:
        return (
            f"sd = {self.intercept:.4f} + {self.slope:.5f} * adp   "
            f"(n={self.n}, R^2={self.r2:.3f})"
        )


def fit_sd_model(players: Iterable[Any]) -> SdFit:
    """Fit ``sd ~ a + b*mu`` by ordinary least squares over players that have a stdev.

    Deliberately not a hardcoded rule of thumb: the coefficients are whatever this league's
    ADP feed actually says, and they are reported so a wrong one is visible.
    """
    xs: list[float] = []
    ys: list[float] = []
    for player in players:
        mu, sd = _mu_sd(player)
        if sd is None or sd <= 0.0:
            continue
        xs.append(mu)
        ys.append(sd)

    n = len(xs)
    if n < 3:
        raise ValueError(f"need >= 3 players with a stdev to fit sd ~ adp, got {n}")

    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    if sxx <= 0.0:
        raise ValueError("every ADP in the fit sample is identical; cannot fit sd ~ adp")
    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x

    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return SdFit(intercept=intercept, slope=slope, n=n, r2=r2)


def load_ffc_adp(path: str | Path | None = None) -> list[AdpPlayer]:
    """Read the newest **cached** FFC payload off disk. No network, ever.

    Draft night runs with wifi off (CLAUDE.md), so this is a pure filesystem read of an
    already-fetched artifact under ``data/raw/ffc/``.
    """
    if path is None:
        pattern = os.path.join(str(REPO_ROOT), "data", "raw", "ffc", "*.json")
        files = sorted(glob.glob(pattern))
        if not files:
            raise FileNotFoundError(f"no cached FFC payload matching {pattern}")
        target = Path(files[-1])
    else:
        target = Path(path)

    with open(target, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    rows = raw.get("players", []) if isinstance(raw, Mapping) else raw

    out: list[AdpPlayer] = []
    for row in rows:
        stdev = row.get("stdev", row.get("std_dev"))
        out.append(
            AdpPlayer(
                player_id=str(row.get("player_id") or row.get("name")),
                name=str(row.get("name", "")),
                pos=str(row.get("position") or row.get("pos") or "").upper(),
                adp=float(row["adp"]),
                stdev=(None if stdev in (None, "") else float(stdev)),
                team=str(row.get("team") or ""),
                bye=row.get("bye"),
            )
        )
    return out


# ------------------------------------------------------------------------- run detection


@dataclass(frozen=True)
class RunReading:
    """One position's run diagnostics at a point in the draft."""

    position: str
    intensity: float
    expected: float
    threshold: float
    share: float
    firing: bool
    shift: float

    def describe(self) -> str:
        verdict = "RUN" if self.firing else "quiet"
        return (
            f"{self.position}: I={self.intensity:.2f} vs expected {self.expected:.2f} "
            f"(share {self.share:.0%}), threshold {self.threshold:.2f} -> {verdict}, "
            f"shift {self.shift:.2f} picks"
        )


class PositionalRun:
    """Exponentially-weighted positional run detector, normalized by board composition.

    Intensity over the last ``window`` picks is ``I_p = sum(decay**age)`` over picks at
    position ``p``, age 0 being the most recent. The comparison point is not a fixed count
    but ``Ibar_p = share_p * sum(decay**age)``, where ``share_p`` is position ``p``'s share
    of the top ``top_n`` remaining players by ADP. That normalization is what makes it usable
    in a 2QB league: when 40% of the best available are quarterbacks, quarterbacks going is
    the null hypothesis, not the alarm.

    A firing run shifts the position's mean ADP **forward** (players go sooner) by
    ``min(max_shift, gain * (I_p - Ibar_p))`` picks. The shift is only re-armed by another
    pick at that position; every intervening pick elsewhere decays it by ``stale_decay``, so
    a run that stops being a run fades out over the next handful of picks instead of hanging
    around distorting the board.
    """

    def __init__(
        self,
        *,
        window: int = 12,
        decay: float = 0.85,
        min_intensity: float = 1.5,
        multiple: float = 2.0,
        gain: float = 4.0,
        max_shift: float = 6.0,
        stale_decay: float = 0.75,
        top_n: int = 30,
    ) -> None:
        if not 0.0 < decay < 1.0:
            raise ValueError(f"decay must be in (0,1), got {decay}")
        if not 0.0 < stale_decay < 1.0:
            raise ValueError(f"stale_decay must be in (0,1), got {stale_decay}")
        self.window = int(window)
        self.decay = float(decay)
        self.min_intensity = float(min_intensity)
        self.multiple = float(multiple)
        self.gain = float(gain)
        self.max_shift = float(max_shift)
        self.stale_decay = float(stale_decay)
        self.top_n = int(top_n)
        self._history: deque[str] = deque(maxlen=self.window)
        self._shift: dict[str, float] = {}

    # ---------------------------------------------------------------- measurement
    @property
    def history(self) -> tuple[str, ...]:
        """Positions of the recent picks, oldest first."""
        return tuple(self._history)

    def total_weight(self) -> float:
        """``sum(decay**age)`` over the whole window -- the denominator of every share."""
        return sum(self.decay**age for age in range(len(self._history)))

    def intensity(self, position: str) -> float:
        pos = str(position).upper()
        # deque is oldest-first, so age counts backwards from the end.
        last = len(self._history) - 1
        return sum(
            self.decay ** (last - i) for i, p in enumerate(self._history) if p == pos
        )

    def share(self, position: str, remaining: Iterable[Any]) -> float:
        """Fraction of the top ``top_n`` remaining players (by ADP) at ``position``."""
        pool = sorted(remaining, key=lambda p: _mu_sd(p)[0])[: self.top_n]
        if not pool:
            return 0.0
        pos = str(position).upper()
        return sum(1 for p in pool if _pos_of(p) == pos) / len(pool)

    def reading(self, position: str, remaining: Iterable[Any]) -> RunReading:
        pos = str(position).upper()
        intensity = self.intensity(pos)
        share = self.share(pos, remaining)
        expected = share * self.total_weight()
        threshold = max(self.min_intensity, self.multiple * expected)
        return RunReading(
            position=pos,
            intensity=intensity,
            expected=expected,
            threshold=threshold,
            share=share,
            firing=intensity >= threshold,
            shift=self.shift(pos),
        )

    def is_running(self, position: str, remaining: Iterable[Any]) -> bool:
        return self.reading(position, remaining).firing

    # ---------------------------------------------------------------------- state
    def observe(self, position: str, remaining: Iterable[Any] | None = None) -> RunReading:
        """Record one drafted pick at ``position`` and update the shifts.

        Args:
            position: the position just taken off the board.
            remaining: the players still available, used to compute the expected share.
                Omit only if you do not care about re-arming (the decay still applies).
        """
        pos = str(position).upper()

        # Every position other than the one just taken gets one pick staler.
        for other in list(self._shift):
            if other != pos:
                self._shift[other] *= self.stale_decay
                if self._shift[other] < 1e-3:
                    del self._shift[other]

        self._history.append(pos)

        if remaining is None:
            return RunReading(pos, self.intensity(pos), 0.0, 0.0, 0.0, False, self.shift(pos))

        pool = list(remaining)
        reading = self.reading(pos, pool)
        if reading.firing:
            # Re-arm from the current measurement. Only a pick AT this position can do this;
            # that is what makes the decay below actually decay.
            self._shift[pos] = min(
                self.max_shift, self.gain * (reading.intensity - reading.expected)
            )
        return self.reading(pos, pool)

    def shift(self, position: str) -> float:
        """Current ADP shift for ``position``, in picks (0 when no run is live)."""
        return self._shift.get(str(position).upper(), 0.0)

    def adjusted_mu(self, mu: float, position: str) -> float:
        """Mean ADP with the live run shift applied -- forward, i.e. sooner."""
        return float(mu) - self.shift(position)

    def reset(self) -> None:
        self._history.clear()
        self._shift.clear()


# ---------------------------------------------------------------------- pool aggregates


def expected_survivors(
    players: Iterable[Any],
    target_pick: float,
    current_pick: float,
    *,
    fit: SdFit | None = None,
    run: PositionalRun | None = None,
) -> float:
    """Expected count of ``players`` still on the board at ``target_pick``.

    The sum of independent conditional survival probabilities. Independence is an
    approximation -- one team taking a quarterback slightly raises the chance another does
    too -- but the mean of a sum is the sum of the means regardless of correlation, so the
    *expected count* is unaffected. Only the spread around it would be.
    """
    total = 0.0
    for player in players:
        mu, sd = _mu_sd(player)
        if run is not None:
            mu = run.adjusted_mu(mu, _pos_of(player))
        total += p_available(mu, sd, target_pick, current_pick, fit=fit)
    return total


def survival_curve(
    players: Sequence[Any],
    picks: Sequence[float],
    current_pick: float,
    *,
    fit: SdFit | None = None,
    run: PositionalRun | None = None,
) -> dict[float, float]:
    """``{pick: expected survivors}`` for a list of future picks. For the cliff table."""
    return {
        int(n) if float(n).is_integer() else n: expected_survivors(
            players, n, current_pick, fit=fit, run=run
        )
        for n in picks
    }


def tier_exhaustion_pick(
    tier_members: Sequence[Any],
    current_pick: float,
    *,
    horizon: int = 120,
    threshold: float = 1.0,
    fit: SdFit | None = None,
    run: PositionalRun | None = None,
) -> int | None:
    """First future pick at which fewer than ``threshold`` members are expected to remain.

    This is the "this tier likely dies in ~7 picks" number. ``None`` means the tier is
    expected to survive the whole horizon -- there is no urgency to manufacture.
    """
    if not tier_members:
        return int(current_pick) + 1
    start = int(math.floor(current_pick)) + 1
    for n in range(start, start + int(horizon)):
        if expected_survivors(tier_members, n, current_pick, fit=fit, run=run) < threshold:
            return n
    return None
