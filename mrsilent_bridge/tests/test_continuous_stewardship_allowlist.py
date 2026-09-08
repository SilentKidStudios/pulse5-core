"""Tests for continuous_stewardship.py's AUTHORIZED_REAL_WORK_GOVERNING_IDS
allowlist — Domain-B Founder authorization (2026-09-08): ranks 2-10 of the
canonical Founder Top-10 order, individually verified, minus rank 1
(OMNISIM_AND_ORACLE_STUDIO_WIDE_ACTIVATION, a separate active Founder gate)
and rank 6 (SCORPIOS_CORNER, protected, explicitly excluded even though
numerically inside "2-10")."""
from __future__ import annotations

import continuous_stewardship as cs
import mission
from evolution import founder_request, proposal as proposal_mod


def _fresh(monkeypatch, tmp_path):
    monkeypatch.setattr(mission, "MISSIONS_DIR", tmp_path / "missions")
    monkeypatch.setattr(proposal_mod, "PROPOSALS_DIR", tmp_path / "proposals")
    monkeypatch.setattr(proposal_mod, "NEW_PROPOSAL_SIGNAL_PATH", tmp_path / "new_proposal_signal")
    monkeypatch.setattr(founder_request, "ESCALATIONS_DIR", tmp_path / "escalations")


def test_allowlist_excludes_rank1_omnisim_and_rank6_scorpio():
    assert "OMNISIM_AND_ORACLE_STUDIO_WIDE_ACTIVATION" not in cs.AUTHORIZED_REAL_WORK_GOVERNING_IDS
    assert "SCORPIOS_CORNER" not in cs.AUTHORIZED_REAL_WORK_GOVERNING_IDS


def test_allowlist_contains_exactly_the_authorized_eight_ranks():
    assert cs.AUTHORIZED_REAL_WORK_GOVERNING_IDS == {
        "MR_SILENT_APP_COMPLETION",
        "MR_SILENT_ADAPTIVE_EVOLUTION_AND_MULTI_BRAIN_WATCH",
        "OMNI_GOD_MODE_INTERFACE_MCP_CLI_CLOSED_LOOP",
        "SILENT_HANDS_WORKER_SWARM",
        "AUTO_MR_SILENT_SELECTS",
        "TH3S1L3NTK1D_STUDIOS_WEBSITE",
        "CONTENT_JOURNEY_OMNIFORGE_PROVIDER_INTEGRATION",
        "PULSEWORLD_REAL_STATE_CONVERGENCE + 3D_4D_MR_SILENT_PHYSICAL_EMBODIMENT",
    }


def test_newly_authorized_rank_gets_one_bounded_founder_gated_proposal(monkeypatch, tmp_path):
    """The real Domain-B mechanism, proven in isolation: a mission for a
    newly-authorized rank with zero existing proposal representation gets
    exactly ONE new, bounded, founder_gated proposal -- never auto-
    actionable, never a dispatched WorkItem, never bypasses anything."""
    _fresh(monkeypatch, tmp_path)
    m = mission.create_mission(
        "Founder Top-10 rank 3: MR_SILENT_ADAPTIVE_EVOLUTION_AND_MULTI_BRAIN_WATCH",
        origin="founder_top10_priority_backlog",
        provenance={"governing_id": "MR_SILENT_ADAPTIVE_EVOLUTION_AND_MULTI_BRAIN_WATCH"},
    )

    result = cs.ensure_proposal_for_founder_priority_mission(m)

    assert result["applicable"] is True
    assert result["action"] == "created_new"
    proposal_id = result["proposal_id"]
    p = proposal_mod.load(proposal_id)
    assert p.risk_score == "founder_gated", "a newly-created proposal from this path must always be founder_gated"
    assert p.status == proposal_mod.ProposalStatus.OBSERVED

    reloaded = mission.load(m.mission_id)
    assert f"proposal:{proposal_id}" in reloaded.root_work_item_ids


def test_ensure_is_idempotent_no_duplicate_proposal(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    m = mission.create_mission(
        "Founder Top-10 rank 5: SILENT_HANDS_WORKER_SWARM",
        origin="founder_top10_priority_backlog",
        provenance={"governing_id": "SILENT_HANDS_WORKER_SWARM"},
    )

    first = cs.ensure_proposal_for_founder_priority_mission(m)
    m = mission.load(m.mission_id)
    second = cs.ensure_proposal_for_founder_priority_mission(m)

    # The created proposal is founder_gated and not yet decision-ready, so
    # the second pass correctly finds it in needs_refinement_open (never
    # founder_gated_open) -- matching this campaign's own F1 fix -- and
    # falls through to the "matches exist but none linkable" noop, not the
    # already-linked branch. Either way, no second proposal is ever made.
    assert second["action"] == "noop"
    assert len(proposal_mod.list_all()) == 1, "a second call must never create a duplicate proposal"
    assert f"proposal:{first['proposal_id']}" in mission.load(m.mission_id).root_work_item_ids


def test_unauthorized_rank_stays_not_applicable(monkeypatch, tmp_path):
    """Rank 11 (OMNIALPHA_OMNITRADE_AND_AUTONOMOUS_REVENUE_ENGINE) was NOT
    part of this Founder authorization (only ranks 2-10, minus rank 6) --
    must remain untouched."""
    _fresh(monkeypatch, tmp_path)
    m = mission.create_mission(
        "Founder Top-10 rank 11: OMNIALPHA_OMNITRADE_AND_AUTONOMOUS_REVENUE_ENGINE",
        origin="founder_top10_priority_backlog",
        provenance={"governing_id": "OMNIALPHA_OMNITRADE_AND_AUTONOMOUS_REVENUE_ENGINE"},
    )
    result = cs.ensure_proposal_for_founder_priority_mission(m)
    assert result["applicable"] is False
    assert "authorized allowlist" in result["reason"]
    assert mission.load(m.mission_id).root_work_item_ids == []
