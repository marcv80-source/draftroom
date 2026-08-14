"""Dynamic positional tiers -- "a tier is defined by who's left" (CLAUDE.md).

Ranking players 1..N is the wrong shape for a live draft. What Marc needs to know is whether
the guy in front of him is in the *same class* as the next three names, or whether taking him
now versus in five picks costs a whole tier. A 1-D Gaussian mixture over draft value answers
that directly: each component is a cluster of players who are roughly interchangeable, and the
gap between clusters is the cliff.

Two failure modes matter more than getting the "right" number of tiers:

**Renumbering.** The pool changes after every single pick, so if tier count is refit from
scratch each time, sampling noise alone will occasionally prefer one more or one fewer
component. A tier flipping from "Tier 2" to "Tier 3" with no real change on the board is the
kind of thing that makes a human stop trusting the tool. :func:`TierEngine.update` only
accepts a change in tier count when the BIC improvement clears a fixed bar (hysteresis), and
because components are always ordered by mean draft value descending, a fit that keeps the
same count also keeps the same labels -- there is nothing to "match" beyond that ordering.

**Small or degenerate pools.** A GMM search needs enough points to be meaningful and a
covariance matrix that isn't singular (duplicate or near-duplicate draft values, common late
in a position's pool). Both cases fall back to largest-gap tiering, which needs no
distributional assumption at all.
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

import numpy as np
from sklearn.exceptions import ConvergenceWarning
from sklearn.mixture import GaussianMixture

__all__ = [
    "TierInfo",
    "TierEngine",
    "fit_tiers",
    "largest_gap_tiers",
    "MIN_K",
    "MAX_K",
    "HYSTERESIS_BIC_GAIN",
    "MIN_N_FOR_GMM",
]

#: Range of candidate tier counts searched by BIC.
MIN_K = 2
MAX_K = 8

#: UNVERIFIED TUNING CONSTANT. A candidate tier count only replaces the previously accepted one
#: when its BIC (on the *current* pool, so the comparison is apples to apples) is at least this
#: much lower. BIC differences below ~2 are conventionally "not worth mentioning"; 6 is chosen
#: to be comfortably past that noise floor so a tier count changes only on a real shift in the
#: pool's shape, not because one player got drafted.
HYSTERESIS_BIC_GAIN = 6.0

#: Below this many remaining players at a position, a GMM fit is more likely to be fitting noise
#: than structure (thin positions like TE exhaust fast). Largest-gap tiering below this size.
MIN_N_FOR_GMM = 8

#: Fallback tier count used by largest-gap tiering, capped at the pool size.
_FALLBACK_K = 3

#: GMM restarts per k. Recompute happens after every pick (a live-draft latency budget, not a
#: one-shot analysis), so this trades a little robustness against local optima for a ~4x speedup
#: measured on the real pool: n_init=4 took ~1.6s on an 81-player WR pool, n_init=2 ~70ms. 1-D
#: k-means init is already a good starting point, so 2 restarts is enough to catch the rare bad
#: initialization without paying for the other's cost.
_GMM_N_INIT = 2


def _dv_of(player: Any) -> float:
    """Pull a scalar draft value out of anything player-ish.

    Accepts :class:`~draftroom.valuation.evob.DraftValue` (field ``dv``), plain mappings, or
    any object exposing ``dv`` / ``draft_value``.
    """
    if isinstance(player, Mapping):
        raw = player.get("dv", player.get("draft_value"))
    else:
        raw = getattr(player, "dv", None)
        if raw is None:
            raw = getattr(player, "draft_value", None)
    if raw is None:
        raise KeyError(f"no draft-value field (dv/draft_value) on {player!r}")
    return float(raw)


# --------------------------------------------------------------------------------- TierInfo


@dataclass(frozen=True)
class TierInfo:
    """One tier: who is in it, and how far the fall is if it empties before you pick again.

    ``cliff`` is the draft-value gap between the *worst* player still in this tier and the
    *best* player in the next tier down -- i.e. what you lose by settling for whoever is left
    in this tier instead of reaching into the one below. ``None`` on the last tier: there is
    nothing further down to fall to.
    """

    tier: int
    members: tuple[Any, ...]
    mean: float
    spread: float
    cliff: float | None = None

    @property
    def size(self) -> int:
        return len(self.members)

    @property
    def best(self) -> Any:
        return self.members[0]

    def describe(self) -> str:
        names = ", ".join(getattr(m, "name", str(m)) for m in self.members[:4])
        more = f" (+{self.size - 4} more)" if self.size > 4 else ""
        cliff_txt = f"cliff {self.cliff:.1f}" if self.cliff is not None else "no cliff (last tier)"
        return f"Tier {self.tier + 1}: {names}{more}  [mean {self.mean:.1f}, {cliff_txt}]"


def _tiers_from_boundaries(
    ranked: Sequence[Any], values: Sequence[float], boundaries: Sequence[int]
) -> list[TierInfo]:
    """Build ``TierInfo`` list given 0-based indices where a new tier starts (excl. index 0)."""
    starts = [0, *boundaries, len(ranked)]
    tiers: list[TierInfo] = []
    for i in range(len(starts) - 1):
        lo, hi = starts[i], starts[i + 1]
        members = tuple(ranked[lo:hi])
        vals = values[lo:hi]
        mean = float(np.mean(vals))
        spread = float(np.std(vals)) if len(vals) > 1 else 0.0
        tiers.append(TierInfo(tier=i, members=members, mean=mean, spread=spread))

    for i in range(len(tiers) - 1):
        worst_here = min(_dv_of(m) for m in tiers[i].members)
        best_next = max(_dv_of(m) for m in tiers[i + 1].members)
        tiers[i] = _with_cliff(tiers[i], worst_here - best_next)
    return tiers


def _with_cliff(t: TierInfo, cliff: float) -> TierInfo:
    return TierInfo(tier=t.tier, members=t.members, mean=t.mean, spread=t.spread, cliff=cliff)


def largest_gap_tiers(players: Sequence[Any], k: int | None = None) -> list[TierInfo]:
    """Tier by the ``k-1`` largest drops in sorted draft value. No distributional assumption.

    Used whenever the pool is too small or too degenerate for a GMM (:data:`MIN_N_FOR_GMM`,
    singular covariance). ``k`` defaults to :data:`_FALLBACK_K`, capped by pool size.
    """
    ranked = sorted(players, key=lambda p: -_dv_of(p))
    n = len(ranked)
    if n == 0:
        return []
    values = [_dv_of(p) for p in ranked]
    k = min(n, _FALLBACK_K if k is None else max(1, k))
    if n == 1 or k <= 1:
        mean = float(values[0])
        return [TierInfo(tier=0, members=tuple(ranked), mean=mean, spread=0.0, cliff=None)]

    gaps = [(values[i] - values[i + 1], i) for i in range(n - 1)]
    boundaries = sorted(idx + 1 for _, idx in sorted(gaps, key=lambda x: -x[0])[: k - 1])
    return _tiers_from_boundaries(ranked, values, boundaries)


def _bic_search(
    values: np.ndarray, k_min: int, k_max: int
) -> dict[int, tuple[GaussianMixture, float]]:
    """Fit a 1-D GMM for every ``k`` in range that is feasible; return each fit's BIC.

    A ``k`` is skipped (not an error) when sklearn cannot fit it: more components than
    distinct values, or a covariance collapse. Callers fall back when this returns empty.
    """
    x = values.reshape(-1, 1)
    n_unique = len(np.unique(values))
    out: dict[int, tuple[GaussianMixture, float]] = {}
    for k in range(k_min, min(k_max, n_unique) + 1):
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", category=ConvergenceWarning)
                gmm = GaussianMixture(
                    n_components=k, random_state=0, n_init=_GMM_N_INIT, reg_covar=1e-6
                )
                gmm.fit(x)
            if not np.all(np.isfinite(gmm.covariances_)):
                continue
            out[k] = (gmm, float(gmm.bic(x)))
        except (ValueError, np.linalg.LinAlgError):
            continue
    return out


def _tiers_from_gmm(gmm: GaussianMixture, players: Sequence[Any], values: np.ndarray) -> list[TierInfo]:
    """Turn a fitted GMM into labeled, ordered, cliff-annotated tiers.

    Component order is re-derived from the fit's own means every time (descending draft
    value), never carried over as an index from a previous fit. That is what makes label
    stability fall out for free whenever the component *count* doesn't change: the same
    cluster of players earns the same rank position among means, recompute after recompute.
    """
    k = gmm.n_components
    labels = gmm.predict(values.reshape(-1, 1))
    means = gmm.means_.reshape(-1)

    order = sorted(range(k), key=lambda c: -means[c])
    rank_of_component = {c: i for i, c in enumerate(order)}

    buckets: list[list[Any]] = [[] for _ in range(k)]
    for player, label in zip(players, labels):
        buckets[rank_of_component[int(label)]].append(player)
    for b in buckets:
        b.sort(key=lambda p: -_dv_of(p))

    tiers: list[TierInfo] = []
    for i, members in enumerate(buckets):
        vals = [_dv_of(p) for p in members]
        mean = float(np.mean(vals)) if vals else float("nan")
        spread = float(np.std(vals)) if len(vals) > 1 else 0.0
        tiers.append(TierInfo(tier=i, members=tuple(members), mean=mean, spread=spread))

    for i in range(len(tiers) - 1):
        if not tiers[i].members or not tiers[i + 1].members:
            continue
        worst_here = min(_dv_of(m) for m in tiers[i].members)
        best_next = max(_dv_of(m) for m in tiers[i + 1].members)
        tiers[i] = _with_cliff(tiers[i], worst_here - best_next)
    return tiers


def fit_tiers(players: Sequence[Any], k: int) -> list[TierInfo]:
    """Tier ``players`` into exactly ``k`` GMM components, ordered by mean draft value descending.

    Raises ``ValueError`` if ``k`` was not a feasible fit -- callers choose ``k`` from
    :func:`_bic_search`'s keys, so this should not happen in normal use.
    """
    ranked_by_input = list(players)
    values = np.array([_dv_of(p) for p in ranked_by_input], dtype=float)
    results = _bic_search(values, k, k)
    if k not in results:
        raise ValueError(f"k={k} was not a feasible GMM fit for this pool (n={len(values)})")
    gmm, _ = results[k]
    return _tiers_from_gmm(gmm, ranked_by_input, values)


# ------------------------------------------------------------------------------- TierEngine


@dataclass
class _PositionState:
    k: int
    bic: float


@dataclass
class TierEngine:
    """Stateful tier fitter with hysteresis, one state slot per position.

    Call :meth:`update` after every pick with the remaining pool at that position. It is safe
    (and expected) to call it for a position nobody just drafted -- the pool there hasn't
    shrunk, but the caller doesn't have to track that; a recompute over a full ~200-player
    position pool measures well under 100ms (see the module test/HANDOFF notes for the real
    number), and BIC search is deterministic given the same pool.
    """

    min_k: int = MIN_K
    max_k: int = MAX_K
    hysteresis_gain: float = HYSTERESIS_BIC_GAIN
    min_n_for_gmm: int = MIN_N_FOR_GMM
    _state: dict[str, _PositionState] = field(default_factory=dict)

    def update(self, position: str, players: Sequence[Any]) -> list[TierInfo]:
        pos = str(position).upper()
        n = len(players)
        if n == 0:
            self._state.pop(pos, None)
            return []

        if n < self.min_n_for_gmm:
            self._state.pop(pos, None)
            return largest_gap_tiers(players)

        values = np.array([_dv_of(p) for p in players], dtype=float)
        results = _bic_search(values, self.min_k, self.max_k)
        if not results:
            self._state.pop(pos, None)
            return largest_gap_tiers(players)

        best_k = min(results, key=lambda kk: results[kk][1])
        prev = self._state.get(pos)

        chosen_k = best_k
        if prev is not None and prev.k in results and best_k != prev.k:
            improvement = results[prev.k][1] - results[best_k][1]
            if improvement < self.hysteresis_gain:
                chosen_k = prev.k

        gmm, bic = results[chosen_k]
        tiers = _tiers_from_gmm(gmm, players, values)
        self._state[pos] = _PositionState(k=chosen_k, bic=bic)
        return tiers

    def reset(self, position: str | None = None) -> None:
        if position is None:
            self._state.clear()
        else:
            self._state.pop(str(position).upper(), None)

    def current_k(self, position: str) -> int | None:
        st = self._state.get(str(position).upper())
        return None if st is None else st.k
