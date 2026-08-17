"""Turning computed numbers into the two or three lines Marc actually reads.

Constraints that shaped this:

- He has maybe four seconds. Anything longer than a line gets skipped, and a paragraph gets ignored
  entirely, which is worse than saying nothing because it buries the one line that mattered.
- Every bullet leads with the number. "18% he's there at 2.04" beats "he probably won't last."
- Only the top two or three fire. A tool that says six things per player is a tool that says nothing.
- The engine never insists. The last bullet is always the way out.

Salience is deliberately crude: each primitive scores how unusual it is right now, and the loudest
two or three win. A cliff worth 2 points shouldn't shout as loudly as one worth 40, and on a normal
pick nothing should shout at all.
"""

from __future__ import annotations

from draftroom.explain.primitives import Candidate, Recommendation, Urgency


def _pct(p: float) -> str:
    """Percentages people say out loud. '18%', not '17.6%'; '<1%' rather than '0%'."""
    if p >= 0.995:
        return ">99%"
    if p > 0 and p < 0.01:
        return "<1%"
    return f"{round(p * 100):.0f}%"


def _pts(x: float) -> str:
    return f"{x:.0f}" if abs(x) >= 10 else f"{x:.1f}"


def _signed(x: float) -> str:
    """Always show the direction. '+0.7' and '-4.2', never a bare '0.7'."""
    return f"+{_pts(x)}" if x > 0 else _pts(x)


# --------------------------------------------------------------------------- individual bullets


def survival_bullet(c: Candidate) -> tuple[str, float, Urgency] | None:
    s = c.survival
    if s.is_gone:
        return (
            f"{_pct(s.p_survive_next)} chance he's there at {s.next_pick_label}. "
            f"Take him now or lose him.",
            9.0,
            Urgency.CRITICAL,
        )
    if s.is_coin_flip:
        return (
            f"Coin flip at {s.next_pick_label} ({_pct(s.p_survive_next)}).",
            6.0,
            Urgency.NOTABLE,
        )
    if s.p_survive_next >= 0.85:
        return (
            f"{_pct(s.p_survive_next)} he's still there at {s.next_pick_label}. No rush.",
            4.0,
            Urgency.CONTEXT,
        )
    return (
        f"{_pct(s.p_survive_next)} he's there at {s.next_pick_label}.",
        5.0,
        Urgency.NOTABLE,
    )


def tier_bullet(c: Candidate) -> tuple[str, float, Urgency] | None:
    t = c.tier
    # A tier boundary only matters if the drop across it is real. Saying "last of his tier" about a
    # two-point gap is how a tool teaches you to ignore it.
    if t.points_to_next_tier < 5:
        return None
    where = ""
    if t.exhaustion_label:
        where = f" This tier is likely gone by {t.exhaustion_label}."
    if t.tier_size_remaining <= 1:
        return (
            f"Last man in his tier. Next {c.pos} down is {_pts(t.points_to_next_tier)} points worse.{where}",
            10.0,
            Urgency.CRITICAL,
        )
    return (
        f"{t.tier_size_remaining} left in this tier, then a {_pts(t.points_to_next_tier)}-point drop.{where}",
        7.0,
        Urgency.NOTABLE,
    )


def pressure_bullet(c: Candidate) -> tuple[str, float, Urgency] | None:
    """The thing no printed cheat sheet can tell him."""
    op = c.opponent_pressure
    if op is None or op.teams_before_next_turn == 0:
        return None
    parts: list[str] = []
    score = 3.0
    if op.teams_needing_position > 0:
        share = op.teams_needing_position / max(1, op.teams_before_next_turn)
        if share >= 1.0:
            # "8 of the 8" reads like an off-by-one even when it is correct.
            parts.append(
                f"all {op.teams_before_next_turn} teams before your next pick still need a "
                f"{op.position}"
            )
        else:
            parts.append(
                f"{op.teams_needing_position} of the {op.teams_before_next_turn} teams before your "
                f"next pick still need a {op.position}"
            )
        score += 6.0 * share
    if op.run_detected:
        parts.append(f"there's a {op.position} run on right now")
        score += 3.0
    if op.league_timing_offset and op.league_timing_offset >= 3:
        parts.append(
            f"and last year this room took {op.position}s about "
            f"{op.league_timing_offset:.0f} picks earlier than the market"
        )
        score += 2.0
    if not parts:
        return None
    text = ", ".join(parts)
    return (text[0].upper() + text[1:] + ".", score, Urgency.NOTABLE)


def depth_bullet(c: Candidate) -> tuple[str, float, Urgency] | None:
    d = c.depth
    if d.is_shutout_risk:
        return (
            f"Only {d.startable_remaining} startable {d.position}s left for "
            f"{d.league_demand_remaining} unfilled slots league-wide. Someone gets shut out.",
            11.0,
            Urgency.CRITICAL,
        )
    if d.picks_of_cushion is not None and d.picks_of_cushion >= 20:
        # "You can wait" is about the POSITION having depth. Never say it next to a line saying this
        # player is about to be gone, or when waiting would cost a whole tier -- the two read as a
        # contradiction, and a panel that argues with itself stops being read at all.
        tier_dies_first = (
            c.tier.exhaustion_pick is not None
            and c.tier.exhaustion_pick <= c.survival.next_pick
            and c.tier.points_to_next_tier >= 5
        )
        if c.survival.is_gone or tier_dies_first:
            return None
        return (
            f"{d.startable_remaining} startable {d.position}s left. You can wait.",
            4.5,
            Urgency.CONTEXT,
        )
    return None


def counterfactual_bullet(c: Candidate) -> tuple[str, float, Urgency] | None:
    cf = c.counterfactual
    if cf is None or abs(cf.net) < 3:
        return None
    if cf.net < 0:
        return (
            f"Costs you about {_pts(abs(cf.net))} points of {cf.position_given_up} "
            f"at your next pick.",
            5.5,
            Urgency.NOTABLE,
        )
    return (
        f"Nets about {_pts(cf.net)} points over taking {cf.position_given_up} first.",
        5.0,
        Urgency.NOTABLE,
    )


def bye_bullet(c: Candidate) -> tuple[str, float, Urgency] | None:
    """Shared byes are a real, arithmetic cost in a short-bench two-QB league.

    Not a correlation argument. If two starters are out the same week and the bench can't cover it,
    that is a loss on the schedule before the season starts.
    """
    if "BYE_COLLISION" in c.flags:
        return (
            f"Shares a week {c.bye} bye with players you've already drafted. "
            f"Short bench, so check you can still field a lineup.",
            7.5,
            Urgency.NOTABLE,
        )
    return None


_BULLET_FNS = (
    depth_bullet,
    tier_bullet,
    survival_bullet,
    pressure_bullet,
    bye_bullet,
    counterfactual_bullet,
)


def _score_bullets(c: Candidate) -> list[tuple[str, float, Urgency]]:
    scored = [got for fn in _BULLET_FNS if (got := fn(c)) is not None]
    scored.sort(key=lambda x: -x[1])
    return scored


def render_candidate(
    c: Candidate, *, max_bullets: int = 3, exclude: frozenset[str] = frozenset()
) -> Candidate:
    """Attach the two or three loudest bullets, plus the way out.

    `exclude` drops board-level facts that were hoisted out by `render` because they were true of
    every candidate.
    """
    scored = _score_bullets(c)
    bullets = [text for text, _, _ in scored if text not in exclude][:max_bullets]

    # The engine informs, it never insists: if he doesn't like this name, the alternatives are
    # already on screen with what they cost. Sign is always explicit -- an unsigned "0.7" next to a
    # "-4.2" reads as a typo, and the direction is the whole point of the number.
    if c.fallbacks:
        alts = ", ".join(
            f"{f.name} ({_signed(-f.points_behind)}, {_pct(f.p_survive_next)} survives)"
            for f in c.fallbacks[:2]
        )
        bullets.append(f"Don't like him: {alts}.")

    c.bullets = tuple(bullets)
    return c


def render(
    rec: Recommendation, *, max_bullets: int = 3, hoist_top_n: int = 4
) -> Recommendation:
    """Render every candidate, hoisting anything true of all the VISIBLE ones to the board level.

    A bullet that fires identically on every option Marc can see tells him nothing about which one
    to pick. It is usually still worth saying once ("nobody here survives to your next turn"), but
    repeating it four times burns the three lines each candidate gets and buries what separates them.

    `hoist_top_n` matters more than it looks. `rec.candidates` holds the best few at EVERY position,
    so an intersection across the whole list is almost always empty: a quarterback and a tight end
    have nothing in common by construction. Only the handful actually on screen compete for Marc's
    attention, so only those decide what is redundant.
    """
    if not rec.candidates:
        return rec

    visible = rec.candidates[:hoist_top_n]
    scored_visible = [_score_bullets(c) for c in visible]

    shared: frozenset[str] = frozenset()
    if len(visible) > 1:
        text_sets = [{text for text, _, _ in s} for s in scored_visible]
        shared = frozenset(set.intersection(*text_sets))

    for c in rec.candidates:
        render_candidate(c, max_bullets=max_bullets, exclude=shared)

    if shared:
        # Preserve salience order rather than set order, so the loudest shared fact reads first.
        hoisted = tuple(text for text, _, _ in scored_visible[0] if text in shared)
        rec.warnings = tuple(rec.warnings) + hoisted
    return rec


def as_text(rec: Recommendation, *, top_n: int = 3) -> str:
    """Plain-text view. Used by the printed cheat sheet and by tests, where reading the actual
    sentences is the only way to tell whether the explanation layer is any good."""
    lines = [f"Pick {rec.pick_label} -- {'YOU' if rec.is_my_pick else f'team {rec.on_the_clock}'}"]
    if rec.at_the_turn:
        lines.append("AT THE TURN: you pick twice in quick succession. Plan the pair.")
    for w in rec.warnings:
        lines.append(f"!! {w}")
    for i, c in enumerate(rec.candidates[:top_n], 1):
        lines.append(f"\n{i}. {c.name} ({c.pos}, {c.team})  value {_pts(c.draft_value)}")
        for b in c.bullets:
            lines.append(f"   - {b}")
    return "\n".join(lines)
