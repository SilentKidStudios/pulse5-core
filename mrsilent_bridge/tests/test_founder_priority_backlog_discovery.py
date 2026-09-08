"""Tests for founder_priority_backlog_discovery.py — Studio-wide
Walk-Away, discovery source #7 (2026-09-08)."""
from __future__ import annotations

import json

import autonomous_cycle as ac
import founder_priority_backlog_discovery as fpbd
import mission


def _fresh(monkeypatch, tmp_path):
    path = tmp_path / "action_queue" / "ACTION-priority-governor-test.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ac, "_CANONICAL_ACTION_AUTHORITY_PATH", path)
    monkeypatch.setattr(mission, "MISSIONS_DIR", tmp_path / "missions")
    return path


def _write_queue(path, entries):
    path.write_text(json.dumps({"durable_priority_queue": entries}))


def test_missing_queue_file_reports_empty_not_crash(monkeypatch, tmp_path):
    _fresh(monkeypatch, tmp_path)
    assert fpbd.read_full_priority_queue() == []
    assert fpbd.discover_backlog() == []


def test_discover_backlog_reflects_real_queue_contents(monkeypatch, tmp_path):
    path = _fresh(monkeypatch, tmp_path)
    _write_queue(path, [
        {"rank": 1, "id": "RANK_ONE", "note": "top priority"},
        {"rank": 2, "id": "RANK_TWO", "note": "second priority"},
    ])
    entries = fpbd.discover_backlog()
    assert len(entries) == 2
    assert entries[0].rank_id == "RANK_ONE"
    assert entries[0].priority == 10.0
    assert entries[1].priority == 9.0


def test_rank_with_existing_mission_is_recognized_not_duplicated(monkeypatch, tmp_path):
    path = _fresh(monkeypatch, tmp_path)
    _write_queue(path, [{"rank": 1, "id": "ALREADY_TRACKED", "note": "already tracked goal"}])
    mission.create_mission("already tracked goal", origin="test")
    entries = fpbd.discover_backlog()
    assert entries[0].existing_mission_id is not None


def test_rank_without_mission_shows_none(monkeypatch, tmp_path):
    path = _fresh(monkeypatch, tmp_path)
    _write_queue(path, [{"rank": 3, "id": "UNREPRESENTED", "note": "nobody tracks this yet"}])
    entries = fpbd.discover_backlog()
    assert entries[0].existing_mission_id is None
    assert entries[0].excluded is False


def test_scorpio_rank_is_excluded_even_without_existing_mission(monkeypatch, tmp_path):
    path = _fresh(monkeypatch, tmp_path)
    _write_queue(path, [{"rank": 6, "id": "SCORPIOS_CORNER", "note": "scorpio goal text"}])
    entries = fpbd.discover_backlog()
    assert entries[0].excluded is True


def test_create_missions_skips_excluded_scorpio_rank(monkeypatch, tmp_path):
    path = _fresh(monkeypatch, tmp_path)
    _write_queue(path, [{"rank": 6, "id": "SCORPIOS_CORNER", "note": "scorpio goal text"}])
    created = fpbd.create_missions_for_unrepresented_ranks()
    assert created == []
    assert mission.list_all() == []


def test_create_missions_skips_already_represented_rank(monkeypatch, tmp_path):
    path = _fresh(monkeypatch, tmp_path)
    _write_queue(path, [{"rank": 1, "id": "ALREADY_TRACKED", "note": "already tracked goal 2"}])
    mission.create_mission("already tracked goal 2", origin="test")
    before = len(mission.list_all())
    created = fpbd.create_missions_for_unrepresented_ranks()
    assert created == []
    assert len(mission.list_all()) == before == 1


def test_create_missions_creates_real_mission_for_unrepresented_rank(monkeypatch, tmp_path):
    path = _fresh(monkeypatch, tmp_path)
    _write_queue(path, [{"rank": 4, "id": "NEW_INITIATIVE", "note": "a real unrepresented Founder priority"}])
    created = fpbd.create_missions_for_unrepresented_ranks()
    assert len(created) == 1
    assert created[0].origin == "founder_top10_priority_backlog"
    assert created[0].provenance["rank_id"] == "NEW_INITIATIVE"
    assert len(mission.list_all()) == 1


def test_created_mission_has_no_root_work_item_registration_only(monkeypatch, tmp_path):
    path = _fresh(monkeypatch, tmp_path)
    _write_queue(path, [{"rank": 5, "id": "REGISTRATION_ONLY", "note": "should not dispatch anything"}])
    created = fpbd.create_missions_for_unrepresented_ranks()
    assert created[0].root_work_item_ids == []


def test_repeated_creation_is_idempotent_no_duplicate_missions(monkeypatch, tmp_path):
    path = _fresh(monkeypatch, tmp_path)
    _write_queue(path, [{"rank": 7, "id": "REPEAT_TEST", "note": "repeated discovery must not duplicate"}])
    c1 = fpbd.create_missions_for_unrepresented_ranks()
    c2 = fpbd.create_missions_for_unrepresented_ranks()
    assert len(c1) == 1
    assert len(c2) == 0
    assert len(mission.list_all()) == 1


def test_multiple_unrepresented_ranks_each_get_own_mission(monkeypatch, tmp_path):
    path = _fresh(monkeypatch, tmp_path)
    _write_queue(path, [
        {"rank": 8, "id": "A", "note": "goal A text"},
        {"rank": 9, "id": "B", "note": "goal B text"},
        {"rank": 6, "id": "SCORPIOS_CORNER", "note": "scorpio goal text 2"},
    ])
    created = fpbd.create_missions_for_unrepresented_ranks()
    assert len(created) == 2
    assert {c.provenance["rank_id"] for c in created} == {"A", "B"}


def test_malformed_queue_entry_without_id_skipped(monkeypatch, tmp_path):
    path = _fresh(monkeypatch, tmp_path)
    _write_queue(path, [{"rank": 1, "note": "no id here"}])
    assert fpbd.discover_backlog() == []
