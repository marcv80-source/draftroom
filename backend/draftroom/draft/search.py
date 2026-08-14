"""Type-ahead player search, ranked by model value among AVAILABLE players.

This is the interaction the whole tool hangs on. Marc is drafting, talking, and half-watching the
board. He types three to five characters and hits Enter. So the ranking rule is not "best string
match" -- it is "of the players who plausibly match what he typed, who is the most valuable one still
on the board". Typing "john" should surface the best available Johnson, not the alphabetically first.

Matching is deliberately forgiving (subsequence + fuzzy) because he is typing fast and looking away.
Ranking is deliberately value-first because the highest-ranked available match is nearly always the
one he means.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

# clean_name, NOT normalize_name: the latter folds nicknames ("jeff" -> "jeffrey"), which is correct
# for joining two complete names across sources and actively wrong for prefix-matching a query the
# user is still halfway through typing.
from draftroom.prep.schema import clean_name


@dataclass(frozen=True)
class SearchablePlayer:
    player_id: str
    name: str
    pos: str
    team: str
    overall_rank: int  # 1 = most valuable. Model-derived, recomputed as the board changes.


@dataclass(frozen=True)
class Match:
    player: SearchablePlayer
    score: float
    reason: str  # which field matched, for debugging a surprising result mid-draft


def _subsequence(needle: str, haystack: str) -> bool:
    """True if every char of `needle` appears in `haystack` in order.

    Catches the way people actually abbreviate under time pressure: 'jjeff' -> 'justin jefferson',
    'ceed' -> 'ceedee lamb'.
    """
    it = iter(haystack)
    return all(c in it for c in needle)


def _field_score(query: str, value: str) -> tuple[float, bool]:
    """Similarity of a query against one field, plus whether it's a prefix hit."""
    if not value:
        return 0.0, False
    if value.startswith(query):
        # Prefix is the strongest signal: it's what typing actually produces.
        return 100.0, True
    if query in value:
        return 88.0, False
    if _subsequence(query, value):
        return 78.0, False
    # Last resort: tolerate real misspelling ('jefersn').
    ratio = fuzz.partial_ratio(query, value)
    return (float(ratio), False) if ratio >= 80 else (0.0, False)


def search(
    query: str,
    players: list[SearchablePlayer],
    *,
    drafted: set[str] | None = None,
    limit: int = 8,
    include_drafted: bool = False,
) -> list[Match]:
    """Rank available players against a partial query.

    `include_drafted` exists for the "wait, did someone already take him?" lookup -- the UI binds it
    to a `~` prefix. Default is available-only, because in the core loop a drafted player showing up
    as the top match is how you record the same guy twice.
    """
    drafted = drafted or set()
    q = clean_name(query).replace(" ", "")
    if not q:
        return []

    pool = players if include_drafted else [p for p in players if p.player_id not in drafted]

    # Rank is a value prior, not a tiebreak. It nudges by up to ~12 points across the whole player
    # universe -- enough to order two equally-good string matches by who is actually worth taking,
    # never enough to let a superstar hijack a clean prefix match on someone else's name.
    def rank_bonus(p: SearchablePlayer) -> float:
        return 12.0 / (1.0 + p.overall_rank / 25.0)

    out: list[Match] = []
    for p in pool:
        norm = clean_name(p.name)
        parts = norm.split()
        last = parts[-1] if parts else ""
        first = parts[0] if parts else ""

        candidates = [
            (last, "last", 1.00),
            (norm.replace(" ", ""), "full", 0.97),
            (first, "first", 0.90),
            (clean_name(p.team), "team", 0.70),
        ]

        best = 0.0
        reason = ""
        for value, label, weight in candidates:
            raw, is_prefix = _field_score(q, value)
            if raw <= 0:
                continue
            s = raw * weight
            # A short query that prefixes a last name is the overwhelmingly common case; make sure
            # it beats a coincidental fuzzy hit somewhere else.
            if is_prefix and label == "last":
                s += 10.0
            if s > best:
                best, reason = s, label

        if best > 0:
            out.append(Match(player=p, score=best + rank_bonus(p), reason=reason))

    out.sort(key=lambda m: (-m.score, m.player.overall_rank))
    return out[:limit]
