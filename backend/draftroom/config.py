"""League configuration -- the object every valuation number is parameterized by.

Nothing in this codebase is allowed to hardcode 12 teams or 2 QBs. Every replacement level,
every man-games demand, every baseline comes out of a :class:`LeagueConfig` built at runtime
from the league's own Yahoo settings (or, until OAuth access lands, from the hand-entered
``data/league_manual.yaml``).

The reason is in CLAUDE.md: 12 teams x 2 QB x 17 weeks = 408 QB-games of demand, which puts
replacement-level QB near QB27 instead of QB17. That gap is the entire edge, and it only
exists if the roster rules flow through the math instead of being assumed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Iterable, Mapping, Sequence

from draftroom.prep.scoring import validate_scoring
from draftroom.prep.stat_map import (
    MissingStatIdError,
    UnsupportedStatIdError,
    resolve_modifier,
)

# MissingStatIdError / UnsupportedStatIdError are re-exported so callers can catch a bad
# league payload without reaching into prep.stat_map themselves.
__all__ = [
    "LeagueConfig",
    "DEFAULT_MANUAL_LEAGUE_PATH",
    "REPO_ROOT",
    "MissingStatIdError",
    "UnsupportedStatIdError",
]

# backend/draftroom/config.py -> backend/draftroom -> backend -> repo root
REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANUAL_LEAGUE_PATH = REPO_ROOT / "data" / "league_manual.yaml"

#: Yahoo writes flex slots as slash-joined single letters ("W/R/T", "Q/W/R/T").
_FLEX_TOKEN_TO_POSITION = {
    "Q": "QB",
    "W": "WR",
    "R": "RB",
    "T": "TE",
    "K": "K",
    "D": "DEF",
}

#: Roster slots that are neither starting slots nor flex: they add no weekly lineup demand.
_NON_STARTING_SLOTS = frozenset({"BN", "IR", "IR+", "IL", "NA"})

#: Yahoo position strings that contain a slash but are NOT flex slots.
_SLASHED_REAL_POSITIONS = {"D/ST": "DEF"}

#: UNVERIFIED. Yahoo settings do not expose a plain "regular season weeks" field; when
#: start_week/end_week are absent we fall back to this. 17 matches the current NFL regular
#: season and CLAUDE.md's working assumption.
DEFAULT_WEEKS = 17


@dataclass(frozen=True)
class LeagueConfig:
    """Immutable snapshot of the league's roster rules and scoring.

    Attributes:
        teams: number of teams in the league.
        starters: position -> required starting slots (excluding flex), e.g. ``{"QB": 2}``.
        flex_slots: number of flex slots per team.
        flex_eligible: positions that may fill a flex slot.
        bench: bench slots per team (IR slots are excluded -- they carry no lineup demand).
        weeks: regular-season weeks the roster must be filled for.
        scoring: canonical stat name -> points per unit.
        draft_slot: our own draft position, 1-based, or ``None`` until known.
    """

    teams: int
    starters: Mapping[str, int]
    flex_slots: int
    flex_eligible: frozenset[str]
    bench: int
    weeks: int
    scoring: Mapping[str, float]
    draft_slot: int | None = None
    #: Free-form notes about values that could not be verified at construction time.
    provenance: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.teams < 2:
            raise ValueError(f"teams must be >= 2, got {self.teams}")
        if self.weeks < 1:
            raise ValueError(f"weeks must be >= 1, got {self.weeks}")
        if self.flex_slots < 0:
            raise ValueError(f"flex_slots must be >= 0, got {self.flex_slots}")
        if self.bench < 0:
            raise ValueError(f"bench must be >= 0, got {self.bench}")

        starters = {str(k).upper(): int(v) for k, v in dict(self.starters).items()}
        if any(v < 0 for v in starters.values()):
            raise ValueError(f"starter counts must be >= 0, got {starters}")
        flex_eligible = frozenset(str(p).upper() for p in self.flex_eligible)
        if self.flex_slots > 0 and not flex_eligible:
            raise ValueError("flex_slots > 0 but flex_eligible is empty")

        scoring = {str(k): float(v) for k, v in dict(self.scoring).items()}
        validate_scoring(scoring)

        if self.draft_slot is not None and not (1 <= self.draft_slot <= self.teams):
            raise ValueError(
                f"draft_slot {self.draft_slot} outside 1..{self.teams}"
            )

        object.__setattr__(self, "starters", MappingProxyType(starters))
        object.__setattr__(self, "flex_eligible", flex_eligible)
        object.__setattr__(self, "scoring", MappingProxyType(scoring))
        object.__setattr__(self, "provenance", tuple(self.provenance))

    # ------------------------------------------------------------------ derived
    @property
    def roster_size(self) -> int:
        """Total roster slots per team: starters + flex + bench."""
        return sum(self.starters.values()) + self.flex_slots + self.bench

    @property
    def positions(self) -> frozenset[str]:
        """Every position that carries lineup demand (starting slots or flex eligibility)."""
        return frozenset(p for p, n in self.starters.items() if n > 0) | (
            self.flex_eligible if self.flex_slots > 0 else frozenset()
        )

    @property
    def total_picks(self) -> int:
        """Roster spots across the whole league -- i.e. how many players get drafted."""
        return self.teams * self.roster_size

    def replace(self, **changes: Any) -> "LeagueConfig":
        """Return a copy with ``changes`` applied. Handy for the parameterization tests."""
        payload: dict[str, Any] = {
            "teams": self.teams,
            "starters": dict(self.starters),
            "flex_slots": self.flex_slots,
            "flex_eligible": self.flex_eligible,
            "bench": self.bench,
            "weeks": self.weeks,
            "scoring": dict(self.scoring),
            "draft_slot": self.draft_slot,
            "provenance": self.provenance,
        }
        payload.update(changes)
        return LeagueConfig(**payload)

    # ------------------------------------------------------------------ loaders
    @classmethod
    def from_yahoo_settings(
        cls,
        settings: Mapping[str, Any],
        stat_map: Mapping[int, str],
        *,
        draft_slot: int | None = None,
        ignore_stat_ids: Iterable[int] = (),
    ) -> "LeagueConfig":
        """Build a config from a Yahoo league-settings payload.

        UNVERIFIED against the live API -- written against the documented shape while OAuth
        access is pending. Handles both a normalized payload (``roster_positions`` as a list
        of ``{"position", "count"}``) and Yahoo's raw XML-transliterated JSON, where
        collections arrive as ``{"0": {...}, "1": {...}, "count": 2}`` and each element is
        wrapped in a singleton dict (``{"roster_position": {...}}``).

        Args:
            settings: the league settings payload.
            stat_map: Yahoo ``stat_id`` -> canonical stat name. Injected rather than imported
                so the map is a visible input to the config, not a hidden global.
            draft_slot: our draft position if already known.
            ignore_stat_ids: ids to drop *deliberately*. Anything not listed here and not in
                the stat map raises; nothing is ever dropped silently.

        Raises:
            MissingStatIdError: a modifier's ``stat_id`` is not in the stat map.
            UnsupportedStatIdError: a recognised id with no canonical stat (e.g. return TDs).
        """
        notes: list[str] = []
        ignored = {int(s) for s in ignore_stat_ids}

        teams = _first_int(settings, ("num_teams", "teams", "team_count"))
        if teams is None:
            raise ValueError("Yahoo settings payload has no team count (num_teams)")

        weeks, weeks_note = _weeks_from_settings(settings)
        if weeks_note:
            notes.append(weeks_note)

        starters, flex_slots, flex_eligible, bench = _parse_roster_positions(
            _normalize_collection(settings.get("roster_positions"), "roster_position")
        )

        scoring: dict[str, float] = {}
        for modifier in _normalize_collection(settings.get("stat_modifiers"), "stat"):
            stat_id = modifier.get("stat_id")
            if stat_id is None:
                raise ValueError(f"stat modifier has no stat_id: {modifier!r}")
            stat_id = int(stat_id)
            value = float(modifier.get("value", 0.0))
            if stat_id in ignored:
                notes.append(f"stat_id {stat_id} dropped by explicit ignore_stat_ids")
                continue
            for name in resolve_modifier(stat_id, stat_map):
                # A composite id (Yahoo's combined 2-pt) writes the same value to each of its
                # canonical targets; that is arithmetically identical to Yahoo's single stat.
                scoring[name] = scoring.get(name, 0.0) + value

        notes.append("built from Yahoo settings; stat_id map UNVERIFIED against live API")
        return cls(
            teams=teams,
            starters=starters,
            flex_slots=flex_slots,
            flex_eligible=flex_eligible,
            bench=bench,
            weeks=weeks,
            scoring=scoring,
            draft_slot=draft_slot,
            provenance=tuple(notes),
        )

    @classmethod
    def from_yaml(cls, path: str | Path | None = None) -> "LeagueConfig":
        """Load the hand-entered fallback config (``data/league_manual.yaml``).

        Everything in that file is PROVISIONAL, read off the Yahoo web UI by hand, and is
        replaced the moment the API loader works.
        """
        import yaml  # Imported lazily so the valuation core has no hard yaml dependency.

        target = Path(path) if path is not None else DEFAULT_MANUAL_LEAGUE_PATH
        with open(target, "r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh) or {}
        if not isinstance(raw, Mapping):
            raise ValueError(f"{target} did not parse to a mapping")

        missing = [k for k in ("teams", "starters", "weeks") if k not in raw]
        if missing:
            raise ValueError(f"{target} missing required key(s): {', '.join(missing)}")

        return cls(
            teams=int(raw["teams"]),
            starters=dict(raw["starters"]),
            flex_slots=int(raw.get("flex_slots", 0)),
            flex_eligible=frozenset(raw.get("flex_eligible", ())),
            bench=int(raw.get("bench", 0)),
            weeks=int(raw["weeks"]),
            scoring=dict(raw.get("scoring", {})),
            draft_slot=(None if raw.get("draft_slot") is None else int(raw["draft_slot"])),
            provenance=(f"PROVISIONAL: hand-entered from {target}",),
        )


# ---------------------------------------------------------------------------- helpers


def _first_int(mapping: Mapping[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        if mapping.get(key) is not None:
            return int(mapping[key])
    return None


def _weeks_from_settings(settings: Mapping[str, Any]) -> tuple[int, str]:
    """Regular-season weeks, from explicit weeks, or start/end week, else the default."""
    explicit = _first_int(settings, ("weeks", "regular_season_weeks"))
    if explicit is not None:
        return explicit, ""

    start = _first_int(settings, ("start_week",))
    end = _first_int(settings, ("end_week",))
    playoff_start = _first_int(settings, ("playoff_start_week",))
    if start is not None and playoff_start is not None:
        # Roster demand is a regular-season quantity: the fantasy playoffs are a different
        # (much smaller) problem and the field is only 4-6 teams.
        return playoff_start - start, "weeks derived as playoff_start_week - start_week"
    if start is not None and end is not None:
        return end - start + 1, "weeks derived as end_week - start_week + 1"
    return DEFAULT_WEEKS, f"UNVERIFIED: weeks defaulted to {DEFAULT_WEEKS} (not in settings)"


def _normalize_collection(raw: Any, element_key: str) -> list[dict[str, Any]]:
    """Flatten a Yahoo collection into a plain list of dicts.

    Accepts a plain list (normalized payload), Yahoo's ``{"0": {...}, "count": n}`` dict
    shape, and the ``{"stats": [...]}`` / ``{"<element_key>": {...}}`` singleton wrappers.
    """
    if raw is None:
        return []
    if isinstance(raw, Mapping):
        # {"stats": [...]} style wrapper around the real collection.
        for wrapper in ("stats", "roster_positions", element_key + "s"):
            if wrapper in raw:
                return _normalize_collection(raw[wrapper], element_key)
        # Yahoo's XML transliteration: numeric string keys plus a "count".
        indexed = sorted(
            ((int(k), v) for k, v in raw.items() if str(k).isdigit()), key=lambda kv: kv[0]
        )
        if indexed:
            count = raw.get("count")
            if count is not None and int(count) != len(indexed):
                raise ValueError(
                    f"Yahoo collection declares count={count} but has {len(indexed)} elements"
                )
            return [_unwrap(v, element_key) for _, v in indexed]
        return [_unwrap(raw, element_key)]
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [_unwrap(v, element_key) for v in raw]
    raise TypeError(f"cannot normalize Yahoo collection of type {type(raw).__name__}")


def _unwrap(element: Any, element_key: str) -> dict[str, Any]:
    """Strip Yahoo's singleton wrapper: ``{"roster_position": {...}}`` -> ``{...}``."""
    if isinstance(element, Mapping) and set(element.keys()) == {element_key}:
        element = element[element_key]
    if not isinstance(element, Mapping):
        raise TypeError(f"Yahoo collection element is not a mapping: {element!r}")
    return dict(element)


def _parse_roster_positions(
    positions: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, int], int, frozenset[str], int]:
    """Split Yahoo roster positions into (starters, flex_slots, flex_eligible, bench)."""
    starters: dict[str, int] = {}
    bench = 0
    flex_groups: dict[frozenset[str], int] = {}

    for entry in positions:
        raw_position = str(entry.get("position", "")).strip().upper()
        if not raw_position:
            raise ValueError(f"roster position entry has no position: {entry!r}")
        count = int(entry.get("count", 0))
        if count <= 0:
            continue

        if raw_position in _NON_STARTING_SLOTS:
            # IR slots hold players who cannot be started, so they add no lineup demand and
            # are deliberately not counted as bench.
            if raw_position == "BN":
                bench += count
            continue

        if raw_position in _SLASHED_REAL_POSITIONS:
            starters[_SLASHED_REAL_POSITIONS[raw_position]] = (
                starters.get(_SLASHED_REAL_POSITIONS[raw_position], 0) + count
            )
            continue

        if "/" in raw_position:
            eligible = frozenset(
                _FLEX_TOKEN_TO_POSITION.get(tok, tok) for tok in raw_position.split("/") if tok
            )
            unknown = {p for p in eligible if p not in _FLEX_TOKEN_TO_POSITION.values()}
            if unknown:
                raise ValueError(
                    f"flex slot {raw_position!r} has unrecognised position token(s): {unknown}"
                )
            flex_groups[eligible] = flex_groups.get(eligible, 0) + count
            continue

        starters[raw_position] = starters.get(raw_position, 0) + count

    if len(flex_groups) > 1:
        raise NotImplementedError(
            "league has multiple distinct flex groups "
            f"({sorted(sorted(g) for g in flex_groups)}); the man-games model allocates one "
            "flex pool and would silently mis-assign demand. Extend "
            "valuation.replacement.man_games_demand before supporting this."
        )
    if flex_groups:
        eligible, slots = next(iter(flex_groups.items()))
    else:
        eligible, slots = frozenset(), 0

    return starters, slots, eligible, bench
