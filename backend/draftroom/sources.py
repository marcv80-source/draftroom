"""Per-source player pools: the seam the server's projection-source toggle imports.

Plan 2026-08-20 B2 (the backend half of A5). One board per projection source, each built once
and cached, so switching the active source on draft night is instant and never touches the disk
or the network again.

Three properties this module exists to guarantee, in order of how badly their absence would
hurt on draft night:

1. **:func:`available_sources` never raises.** It runs at server startup. A source that cannot
   build -- a missing ESPN cache, a stale FantasyPros CSV, a crosswalk hiccup -- is reported
   with ``player_count`` 0 and a note saying what went wrong, because a header control that
   fails to render is a worse outcome than a source listed as unavailable.
2. **Every board is cached per key.** Building one is ~0.5s against the cached payloads; the
   whole set is a couple of seconds at startup, and zero on every switch after that.
3. **No network, ever.** Everything here goes through
   :func:`draftroom.live_data.load_player_pool`, which reads only ``data/raw/`` caches. Draft
   night runs with wifi physically off and ``install_socket_guard`` enforces that at runtime.

What the keys MEAN, since the whole point of the toggle is an honest comparison:

* ``blend`` -- the equal-weight composite of the four independent families' COMPONENT STATS,
  scored once under the league's own modifiers (:mod:`draftroom.valuation.composite`). The
  default, per Marc's 2026-08-20 decision.
* ``sleeper`` / ``espn`` / ``fantasypros`` / ``fantasysharks`` -- that source's statline
  **unmodified**. Not a re-weighting of the blend, not the blend with one source emphasised:
  the source, as it is.

Player counts differ by key and that is a real fact about the sources, not a bug: a source that
has no projection for a ranked player cannot value them, and the board records the miss rather
than back-filling from a different source (which would make the comparison meaningless).
"""

from __future__ import annotations

import logging
from typing import Any, Sequence

from draftroom import live_data

__all__ = [
    "SOURCE_KEYS",
    "SOURCE_LABELS",
    "DEFAULT_SOURCE",
    "available_sources",
    "pool_for_source",
    "pool_for_source_strict",
    "real_values_in",
    "SourceUnavailable",
    "clear_cache",
]

log = logging.getLogger("draftroom.sources")

#: Valid source keys, in the order the UI should offer them. Mirrors
#: :data:`draftroom.validate.board.BOARD_SOURCE_KEYS`; kept as a literal here so importing this
#: module never drags in the valuation pipeline just to enumerate the keys (a test pins the two
#: tuples together).
SOURCE_KEYS: tuple[str, ...] = (
    "blend", "sleeper", "espn", "fantasypros", "fantasysharks",
)

DEFAULT_SOURCE: str = live_data.DEFAULT_SOURCE

SOURCE_LABELS: dict[str, str] = {
    "blend": "Blend (4-source)",
    "sleeper": "Sleeper",
    # Named for what it is: CLAUDE.md verified ESPN's API and Mike Clay's draft-kit PDF are ONE
    # source (411 of 411 players identical field-for-field), because Clay is ESPN's projections
    # analyst. Labelling them separately anywhere would invite double-counting.
    "espn": "ESPN (Mike Clay)",
    "fantasypros": "FantasyPros",
    "fantasysharks": "FantasySharks",
}

SOURCE_NOTES: dict[str, str] = {
    "blend": (
        "RECOMMENDED. Equal weight across Sleeper, ESPN, FantasyPros and FantasySharks, blended "
        "on COMPONENT STATS and scored once under this league's own modifiers. Each stat is "
        "averaged only over the sources that publish it, so a stat only one source has is that "
        "source's number, never divided by four; targets now come from TWO sources (ESPN and "
        "FantasySharks, which disagree by a mean of 12 targets on 359 shared players) rather "
        "than one; games comes from ESPN alone, because Sleeper's is a blanket 18.0 and neither "
        "FantasyPros nor FantasySharks publishes a games column at all. Equal weight because no "
        "source has an earned track record here: 2025 preseason projections per source are not "
        "retrievable, so weighting by measured accuracy cannot be justified yet."
    ),
    "sleeper": (
        "Sleeper's season projections alone -- the board's sole projection source before "
        "2026-08-20. Publishes no receiving targets at all (0 of 3,111 records) and reports a "
        "flat 18.0 games for every player, which is one more than this league even plays."
    ),
    "espn": (
        "ESPN's public league-defaults projections (Mike Clay's numbers) alone. The only source "
        "with a real per-player games figure (seven distinct values, including 4- and 6-game "
        "flags on specific players). It was also the only source with projected targets until "
        "FantasySharks was verified and wired in on 2026-08-20; the two now disagree on targets "
        "by a mean of 12 on 359 shared players, which is what makes averaging that stat mean "
        "anything."
    ),
    "fantasypros": (
        "UNRELIABLE STANDALONE -- read it as a cross-check, not as a board. FantasyPros' "
        "component stats are sound and it contributes to the blend on equal terms, but its "
        "export has NO games column, so on this standalone board every player's games volume "
        "comes from the fitted rank-conditional availability prior rather than from the source. "
        "The measured consequence, on the 2026-08-20 cached data: the top QB falls to overall "
        "#12 and only 1 QB reaches the top 30, against #9 and 5 QBs on the blend -- i.e. this "
        "board loses the 2-QB positional shift that is the entire edge in this league. It also "
        "publishes no targets and no 2-point conversions."
    ),
    "fantasysharks": (
        "The FOURTH independent family, verified 2026-08-20 against two controls before it was "
        "allowed near any consensus measure (docs/FANTASYSHARKS.md): the same machinery scores "
        "the ESPN-vs-Mike-Clay pair at 99.8% identical (one source re-published) and puts "
        "FantasySharks at 0.0-0.2% against all three incumbents. Its contribution is targets "
        "(rec_tgt on 427 of 516 players, which took that stat from one source to two) and "
        "projected counts of games clearing each yardage threshold -- the first external "
        "reference the bonus model has ever had. Like FantasyPros it publishes NO games column, "
        "so on this standalone board every player's games volume comes from the fitted "
        "rank-conditional availability prior; unlike FantasyPros it publishes no 2-point "
        "conversions either, and no rushing ATTEMPTS for WR/TE."
    ),
}

# key -> pool, populated lazily. Boards are immutable snapshots of cached data, so caching them
# for the process lifetime is safe; `clear_cache` exists for tests and for a prep re-run.
_POOL_CACHE: dict[str, list[live_data.PoolPlayer]] = {}
_SOURCES_CACHE: list[dict[str, Any]] | None = None


def clear_cache() -> None:
    """Drop every cached pool. Call after a prep refresh; tests use it for isolation."""
    global _SOURCES_CACHE
    _POOL_CACHE.clear()
    _SOURCES_CACHE = None


class SourceUnavailable(RuntimeError):
    """This source's pool built, but carries no real board values.

    Distinct from ``ValueError`` for an unknown key: the key is fine, the DATA is not. Serving
    this pool anyway put ADP placeholders on screen under a real source's name, wrote a
    ``source_changed`` event naming that source, and survived a relaunch -- so the log claimed
    picks were made against a board that never loaded (Codex 2026-08-21 finding 5).
    """


def pool_for_source(key: str) -> list[live_data.PoolPlayer]:
    """The full player pool (ranked + unranked) valued under ``key``, cached per key.

    Raises:
        ValueError: unknown key. Falling back to some other source would put a board on screen
            that is not the board the label claims -- and a pick would then be recorded against
            a projection nobody chose.
    """
    if key not in SOURCE_KEYS:
        raise ValueError(
            f"unknown projection source {key!r}; expected one of {list(SOURCE_KEYS)}"
        )
    cached = _POOL_CACHE.get(key)
    if cached is None:
        cached = live_data.load_player_pool(source=key)
        _POOL_CACHE[key] = cached
    return cached


def real_values_in(pool: Sequence[live_data.PoolPlayer]) -> int:
    """How many ADP-ranked players this pool could actually VALUE. Zero means placeholder mode."""
    return sum(1 for p in pool if p.is_ranked and p.value_is_real)


def pool_for_source_strict(key: str) -> list[live_data.PoolPlayer]:
    """:func:`pool_for_source`, but refuses to hand back a placeholder pool.

    Use this on any path that makes a source ACTIVE -- the toggle and the mid-draft resume. The
    lenient version stays for :func:`available_sources`, whose entire job is to describe broken
    sources without breaking.

    Raises:
        ValueError: unknown key.
        SourceUnavailable: the pool built but valued nothing.
    """
    pool = pool_for_source(key)
    valued = real_values_in(pool)
    if valued == 0:
        ranked = sum(1 for p in pool if p.is_ranked)
        raise SourceUnavailable(
            f"source {key!r} valued 0 of {ranked} ranked players -- its board did not join, so "
            "the pool is ADP-placeholder fallback. Refusing to serve it under this source's "
            "name; run prep to restore the cache."
        )
    return pool


def available_sources() -> list[dict[str, Any]]:
    """One entry per source: ``{key, label, player_count, total_count, note, available}``.

    ``player_count`` is the number of ADP-ranked players this source could actually VALUE (a
    real board value joined on), which is the number that answers "how much of the board does
    this source cover". ``total_count`` is the whole pool including the roster-only tier that
    exists purely so the board can record a name (CLAUDE.md: "The player pool is TWO TIERS").

    Never raises, and safe to call at startup: a source that cannot build is reported with
    ``player_count`` 0, ``available`` False, and a note carrying the failure, so the UI can grey
    it out instead of the whole control disappearing.
    """
    global _SOURCES_CACHE
    if _SOURCES_CACHE is not None:
        return [dict(entry) for entry in _SOURCES_CACHE]

    out: list[dict[str, Any]] = []
    for key in SOURCE_KEYS:
        note = SOURCE_NOTES.get(key, "")
        try:
            pool = pool_for_source(key)
        except Exception as exc:  # noqa: BLE001 - a broken source must not break the toggle
            log.warning("projection source %r unavailable: %s: %s", key, type(exc).__name__, exc)
            out.append(
                {
                    "key": key,
                    "label": SOURCE_LABELS.get(key, key),
                    "player_count": 0,
                    "total_count": 0,
                    "available": False,
                    "note": f"UNAVAILABLE ({type(exc).__name__}: {exc}). {note}",
                }
            )
            continue

        valued = real_values_in(pool)
        ranked = sum(1 for p in pool if p.is_ranked)
        available = valued > 0
        detail = note
        if not available:
            detail = (
                "UNAVAILABLE (built, but valued 0 of "
                f"{ranked} ranked players -- the real board did not join, so this pool is in "
                f"ADP-placeholder fallback mode). {note}"
            )
        elif valued < ranked:
            detail = (
                f"{note} Valued {valued} of {ranked} ranked players; the rest are listed for "
                "bookkeeping with no projection."
            )
        out.append(
            {
                "key": key,
                "label": SOURCE_LABELS.get(key, key),
                "player_count": valued,
                "total_count": len(pool),
                "available": available,
                "note": detail,
            }
        )

    _SOURCES_CACHE = out
    return [dict(entry) for entry in out]
