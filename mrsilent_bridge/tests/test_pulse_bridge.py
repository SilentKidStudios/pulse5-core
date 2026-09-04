#!/usr/bin/env python3
"""Tests for evolution/pulse_bridge.py and its wiring into
evolution/founder_conversation.py (App/Command Orchestration milestone,
Phase 5, Founder-authorized 2026-08-20).

MR_SILENT_LOGICAL_ENTITY_COUNT=1: Pulse's real, existing, authoritative
unified approval inbox (mr_silent_spine/master_approval_unification_v1)
is read and routed to directly -- never copied into a second database on
Render. The parser tests below are pure/offline (a canned sample of the
REAL renderer's actual output format, captured live from pulse5-core-01);
the one live test at the end proves the real merge against the real,
currently-reachable Pulse host.

Run: python3 tests/test_pulse_bridge.py
"""
from __future__ import annotations

import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evolution import pulse_bridge
from evolution import founder_conversation as fc

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


# Real output captured live from pulse5-core-01's arc210_unified_pending_renderer.py, 2026-08-20.
_SAMPLE_RENDERER_OUTPUT = """\U0001F9E0 MR. SILENT — MASTER PENDING

Total shown: 3

1. [OS] FOUNDER_SIGNAL_08a8601641c6c71a
   Risk: Tier2
2. [FA] gap_62731c92c7
   Risk: Tier1
3. [RG] rg_example_ref
   Risk: Tier3

Actions:
• /approve 1
• /deny 1
• /details 1

One list. One approve. One deny.
"""


def test_parser_extracts_all_real_items_with_correct_fields() -> None:
    items = pulse_bridge._parse_renderer_output(_SAMPLE_RENDERER_OUTPUT)
    check("all 3 sample items parsed", len(items) == 3, items)
    check("item 1 has the real OS tag and id", items[0]["tag"] == "OS" and items[0]["pulse_id"] == "FOUNDER_SIGNAL_08a8601641c6c71a", items[0])
    check("item 1's approval_id is tag-id joined with a dash (matches bot.py's real OS-/FA-/RG- prefix convention)",
          items[0]["approval_id"] == "OS-FOUNDER_SIGNAL_08a8601641c6c71a", items[0])
    check("item 1's risk was captured from the following line", items[0].get("risk") == "Tier2", items[0])
    check("every item is tagged source_authority=pulse", all(i["source_authority"] == "pulse" for i in items))
    check("positions are 1-indexed matching the real /approve N convention", [i["position"] for i in items] == [1, 2, 3], items)


def test_dispatch_script_lookup_matches_bots_own_real_table() -> None:
    check("OS tag routes to the real OS adapter", pulse_bridge.dispatch_script_for_tag("OS") == pulse_bridge.DISPATCH_BY_TAG["OS"])
    check("FA tag routes to the real FA adapter", pulse_bridge.dispatch_script_for_tag("FA") == pulse_bridge.DISPATCH_BY_TAG["FA"])
    check("RG tag routes to the real RG adapter", pulse_bridge.dispatch_script_for_tag("RG") == pulse_bridge.DISPATCH_BY_TAG["RG"])
    check("an unrecognized tag falls back to the real unified router (never guesses a new path)",
          pulse_bridge.dispatch_script_for_tag("ZZ") == pulse_bridge.DEFAULT_DISPATCH)


def test_combined_pending_merges_both_sources_without_copying_either() -> None:
    """Mocked pulse_bridge (deterministic) — proves the MERGE logic itself,
    independent of Pulse's real, ever-changing live backlog."""
    original = pulse_bridge.list_pulse_pending
    pulse_bridge.list_pulse_pending = lambda: [
        {"source_authority": "pulse", "source_node": "pulse5-core-01", "approval_id": "OS-synthetic_test_item",
         "tag": "OS", "pulse_id": "synthetic_test_item", "position": 1, "status": "AWAITING_APPROVAL", "risk": "Tier2"}
    ]
    try:
        items, reachable, error = fc.combined_pending()
        check("Pulse reachability reported True on a clean mocked call", reachable is True)
        check("no error recorded on a clean call", error is None)
        check("the mocked Pulse item is present in the combined view", any(i.get("approval_id") == "OS-synthetic_test_item" for i in items), items)
        check("Pulse items retain source_authority=pulse (never silently relabeled render)",
              next(i for i in items if i["approval_id"] == "OS-synthetic_test_item")["source_authority"] == "pulse")
    finally:
        pulse_bridge.list_pulse_pending = original


def test_combined_pending_reports_unreachable_honestly_never_silent() -> None:
    """A Pulse failure must never be silently read as 'zero pending there' —
    proves Render's own (real, local) items still come back even when
    Pulse can't be reached, with reachable=False and a real error string."""
    original = pulse_bridge.list_pulse_pending
    def _boom():
        raise pulse_bridge.PulseUnreachableError("synthetic test: simulated unreachable")
    pulse_bridge.list_pulse_pending = _boom
    try:
        items, reachable, error = fc.combined_pending()
        check("Pulse reachability reported False on a real failure", reachable is False)
        check("a real, non-empty error string is recorded", bool(error) and "synthetic test" in error, error)
        check("the function did not raise — Render's own list is still usable", isinstance(items, list))
    finally:
        pulse_bridge.list_pulse_pending = original


def test_ssh_remote_command_preserves_argv_with_spaces_and_shell_metachars() -> None:
    """Real bug reproduced live 2026-09-04 against escalation
    0d2e162be10f83be: `ssh host a b c` does not preserve argv boundaries --
    ssh joins trailing args with spaces and the remote shell re-tokenizes
    them, which silently word-split a free-text audit note
    ("resolved via MR. SILENT App approvals UI") down to just its first
    word ("resolved") by the time it reached founder_request.py's CLI.
    Offline, no live SSH needed: shlex.split() over _build_remote_command()'s
    output simulates exactly what a POSIX remote shell reconstructs, and
    must reproduce the ORIGINAL argv exactly, including arguments containing
    spaces, semicolons, $() command substitution, backticks, and quotes."""
    argv = [
        "python3", "/opt/pulse5-core/mrsilent_bridge/evolution/founder_request.py",
        "resolve", "0d2e162be10f83be", "approved",
        "resolved via MR. SILENT App approvals UI",
    ]
    reconstructed = shlex.split(pulse_bridge._build_remote_command(argv))
    check("multi-word audit note survives the round trip intact (the exact reproduced failure)",
          reconstructed == argv, reconstructed)

    hostile_argv = ["echo", "note with spaces; $(whoami) `id` \"quoted\" 'single' & | > /tmp/x"]
    reconstructed2 = shlex.split(pulse_bridge._build_remote_command(hostile_argv))
    check("shell metacharacters (; $() ` \" ' & | >) survive as literal text, never interpreted by the remote shell",
          reconstructed2 == hostile_argv, reconstructed2)

    unicode_argv = ["echo", "unicode note: café — 日本語 — emoji 🚀 with, punctuation!"]
    reconstructed3 = shlex.split(pulse_bridge._build_remote_command(unicode_argv))
    check("unicode/spacing/punctuation preserved exactly",
          reconstructed3 == unicode_argv, reconstructed3)


def test_live_pulse_pending_reachable_and_real() -> None:
    """The one real, live acceptance check: the actual pulse5-core-01 host,
    over the actual established SSH bridge, right now. Read-only —
    approves/denies nothing."""
    try:
        items = pulse_bridge.list_pulse_pending()
    except pulse_bridge.PulseUnreachableError as e:
        check("live Pulse pending list is reachable", False, f"Pulse unreachable during this test run: {e}")
        return
    check("the live call returned a list", isinstance(items, list))
    check("every real item has a real source_authority=pulse tag", all(i.get("source_authority") == "pulse" for i in items), items[:2])
    check("every real item has a non-empty real approval_id", all(i.get("approval_id") for i in items), items[:2])


if __name__ == "__main__":
    test_parser_extracts_all_real_items_with_correct_fields()
    test_dispatch_script_lookup_matches_bots_own_real_table()
    test_combined_pending_merges_both_sources_without_copying_either()
    test_combined_pending_reports_unreachable_honestly_never_silent()
    test_ssh_remote_command_preserves_argv_with_spaces_and_shell_metachars()
    test_live_pulse_pending_reachable_and_real()

    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print("\nALL TESTS PASSED")
