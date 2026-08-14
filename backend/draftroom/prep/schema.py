"""Canonical stat vocabulary and shared data shapes for the prep pipeline.

Every source adapter maps its own field names into CANONICAL_STATS at ingest.
Nothing downstream of this module should ever see a source-specific field name.
See CLAUDE.md ("Canonical stat vocabulary") for the single source of truth.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, fields

# Exactly the vocabulary listed in CLAUDE.md, in the order given there.
CANONICAL_STATS: tuple[str, ...] = (
    "pass_att",
    "pass_cmp",
    "pass_yd",
    "pass_td",
    "pass_int",
    "pass_2pt",
    "rush_att",
    "rush_yd",
    "rush_td",
    "rush_2pt",
    "rec",
    "rec_tgt",
    "rec_yd",
    "rec_td",
    "rec_2pt",
    "fum_lost",
    "games",
)


@dataclass
class StatLine:
    """One player-season of component stats. Never fantasy points."""

    pass_att: float = 0.0
    pass_cmp: float = 0.0
    pass_yd: float = 0.0
    pass_td: float = 0.0
    pass_int: float = 0.0
    pass_2pt: float = 0.0
    rush_att: float = 0.0
    rush_yd: float = 0.0
    rush_td: float = 0.0
    rush_2pt: float = 0.0
    rec: float = 0.0
    rec_tgt: float = 0.0
    rec_yd: float = 0.0
    rec_td: float = 0.0
    rec_2pt: float = 0.0
    fum_lost: float = 0.0
    games: float = 0.0

    def as_dict(self) -> dict[str, float]:
        return {name: getattr(self, name) for name in CANONICAL_STATS}

    def has_nonzero_stats(self) -> bool:
        """True if any component stat (excluding `games`) is nonzero."""
        return any(getattr(self, name) for name in CANONICAL_STATS if name != "games")


# Defensive check: StatLine's fields must exactly match CANONICAL_STATS, in order.
# If someone edits one without the other, fail loudly at import time rather than
# silently dropping a stat somewhere downstream.
_statline_field_names = tuple(f.name for f in fields(StatLine))
if _statline_field_names != CANONICAL_STATS:
    raise AssertionError(
        f"StatLine fields {_statline_field_names} do not match CANONICAL_STATS {CANONICAL_STATS}"
    )


@dataclass
class PlayerRef:
    """A player as known by one source."""

    name: str
    pos: str
    team: str
    source_id: str
    source: str


# ---------------------------------------------------------------------------
# Name normalization (used by the crosswalk to match players across sources)
# ---------------------------------------------------------------------------

_SUFFIX_TOKENS = {"jr", "sr", "ii", "iii", "iv", "v"}

# First-token nickname folding: normalize a common short form to the full
# given name so "Mike Evans" and "Michael Evans" collapse to the same key.
_NICKNAME_FOLD: dict[str, str] = {
    "mike": "michael",
    "ken": "kenneth",
    "josh": "joshua",
    "rob": "robert",
    "chris": "christopher",
    "cam": "cameron",
    "will": "william",
    "matt": "matthew",
    "nick": "nicholas",
    "tony": "anthony",
    "ben": "benjamin",
    "greg": "gregory",
    "jeff": "jeffrey",
    "steve": "steven",
    "dave": "david",
    "zack": "zachary",
    "gabe": "gabriel",
    "marv": "marvin",
}

# Known football alias cases: a player commonly listed under a nickname that
# does NOT derive from their given first name via simple folding above (so
# _NICKNAME_FOLD alone can't catch it). Keys/values are raw display names;
# both sides run through the same base cleaning before comparison, so casing
# and punctuation here don't matter. Extend this dict as new cases turn up.
_ALIAS_TABLE: dict[str, str] = {
    "hollywood brown": "marquise brown",
    "chig okonkwo": "chigoziem okonkwo",
    "tank dell": "nathaniel dell",
    "deebo samuel": "tyshun samuel",
    "bam knight": "zonovan knight",
}


def _basic_clean(name: str) -> str:
    s = name.lower()
    s = s.replace("-", " ")
    s = re.sub(r"[^a-z0-9\s]", "", s)  # strip punctuation: periods, apostrophes, commas, etc.
    s = re.sub(r"\s+", " ", s).strip()
    tokens = s.split(" ") if s else []
    while tokens and tokens[-1] in _SUFFIX_TOKENS:
        tokens.pop()
    return " ".join(tokens)


def clean_name(name: str) -> str:
    """Lowercase, strip punctuation and generational suffixes. No nickname folding.

    This is the right normalization for PREFIX matching against a partially-typed query.
    `normalize_name` is deliberately not usable there: folding rewrites a half-typed token into a
    different word ("jeff" -> "jeffrey"), which silently breaks matching on any name that merely
    starts with those letters. Folding is for joining two complete names; cleaning is for typing.
    """
    return _basic_clean(name)


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation/suffixes, fold nicknames and known aliases.

    For crosswalk joins between two COMPLETE names. Do not use on partial search input --
    see `clean_name`.

    Used by the crosswalk to match the same player across sources that
    spell/format names differently (e.g. "Marquise 'Hollywood' Brown" vs.
    "Hollywood Brown" vs. "Michael Pittman Jr." vs. "Michael Pittman").
    """
    if not name:
        return ""
    cleaned = _basic_clean(name)
    if cleaned in _ALIAS_TABLE:
        cleaned = _basic_clean(_ALIAS_TABLE[cleaned])
    tokens = cleaned.split(" ") if cleaned else []
    if tokens:
        tokens[0] = _NICKNAME_FOLD.get(tokens[0], tokens[0])
    return " ".join(tokens)
