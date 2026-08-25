# Archive — settled, not a live reference

Everything in this folder is **finished**. It is kept because a decision was made on the strength
of it and the evidence should survive; it is NOT a description of how the system works today, and
nothing here should be read as current.

Moved here 2026-08-25 by an audit whose only yardstick was the tool's four purposes: record picks
live, show who is available, recommend, and source projections.

| File | What it settled | Status |
|---|---|---|
| `SOURCE_BACKTEST.md` | Source weighting. Best-weight bootstrap interval 0.30–1.00 on one season, so **equal weight** ships. | Decided. Do not reweight without multi-season evidence. |
| `RENORMALIZATION_VERDICT.md` | Team-identity renormalization. Improved 2025 MAE, then **lost to a flat haircut of equal magnitude**, and rank ordering got worse. **Declined.** | Decided. This is the origin of the beat-a-dumb-null rule. |
| `PROJECTION_CHALLENGES.md` | The catalogue of projection defects that produced the review queue's detectors. | Absorbed into `valuation/candidates.py` and `docs/REVIEW_QUEUE.md`. |
| `PLAN_2026-08-20.md` | The implementation plan for the four-source composite and the UI rebuild. | Executed. Describes a Sleeper-only board that no longer exists. |
| `YAHOO_SETUP.md` | How to obtain and use Yahoo API access. | **Dead.** Access was never granted and is not being pursued (Marc, 2026-08-25). It also references a `prep/yahoo_client.py` that never existed. |

**The code that generated this evidence was deleted, not the evidence.** `tools/backtest_sources.py`
and `tools/check_renormalization.py` (3,446 lines between them) were removed on 2026-08-25 because
they only kept re-asking questions that had been answered, and their tests were the only red in the
suite. Git history holds them if a multi-season study is ever worth doing.
