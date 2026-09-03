"""Bounded Prioritization (Phase Q).

Orders eligible proposals before evolution/advance.py's advance_eligible()
processes them — it does NOT change which proposals are eligible (that's
still _eligible()'s job: risk_score=='low', or 'founder_gated' with an
exact canonical Founder approval, plus an open status) or how many get
processed per cycle (still MAX_PROPOSALS_PER_CYCLE). This only changes
the ORDER, so limited job budget is spent on the highest-evidence work
first — "prefer fixing one root cause affecting five divisions over five
independent symptom patches" becomes real once root-cause-correlated
proposals (more source_observation_ids = more raw signals a single fix
would resolve) sort ahead of single-signal ones.

Factors used, all real and already on the Proposal record — nothing
hardcoded/invented:
  1. explicit Founder priority (proposal.founder_priority) — ALWAYS wins
     over any inferred score, per the Founder's own instruction.
  2. recurrence/breadth: len(source_observation_ids) — a proposal born from
     evolution/observe.py's multi-signal root-cause correlation naturally
     has more of these than a single raw symptom; more = broader impact if
     fixed, so it sorts first among inferred-priority proposals.
  3. age (older first) as the final tiebreak — never let a proposal starve
     indefinitely just because newer ones keep recurring with similar breadth.
"""
from __future__ import annotations

from evolution import proposal as proposal_mod


def priority_key(p: proposal_mod.Proposal) -> tuple:
    """Sort key — LOWER sorts first (= processed sooner). Use with
    sorted(candidates, key=priority_key)."""
    if p.founder_priority is not None:
        return (0, -p.founder_priority, p.created_at)
    recurrence = len(p.source_observation_ids)
    return (1, -recurrence, p.created_at)


def rank(candidates: list[proposal_mod.Proposal]) -> list[proposal_mod.Proposal]:
    """Pure, side-effect-free — same shape as studio_router.rank()."""
    return sorted(candidates, key=priority_key)
