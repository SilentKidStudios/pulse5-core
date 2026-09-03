"""pytest-invocation counterpart to tests/_test_isolation.py's
job_ledger teardown (2026-09-03, PYTEST_LEDGER_ISOLATION gap-closure).

Root cause: isolated_test_state() is a plain context manager, only ever
invoked by a test file's own `if __name__ == "__main__": with
isolated_test_state(): ...` block -- see its own module docstring. pytest
NEVER executes that block (it imports the module and calls each test_*
function directly; `__main__` guards exist specifically to prevent that),
so every test collected and run via pytest gets zero isolation, even
though 16 files import isolated_test_state() expecting it. Mechanically
proven (2026-09-03): running tests/test_job_ledger_active_work_precheck.py
etc. via `pytest` left 4 new non-terminal job_ledger fixture records behind
every single time, while the identical `python3 tests/<file>.py` direct
invocation left zero, twice in a row.

USES isolated_job_ledger_state(), NOT the full isolated_test_state() --
deliberately. isolated_test_state() also imports campaign, evolution.
founder_request, and live_sensor_governance, all three themselves
untracked in this repo as of this pass; fine for the pre-existing
`__main__`-block usage (those files already exist wherever isolated_test_
state() itself does in this exact working environment), but wrong for a
tracked conftest.py, which must stay importable from a clean checkout
using only tracked dependencies. isolated_job_ledger_state() depends on
nothing but job_ledger (tracked) and _test_isolation's own tracked
helpers -- see its docstring in _test_isolation.py. This also keeps this
fixture's own scope matched to what this pass actually set out to fix
(the job_ledger leak specifically), not a broader pytest-isolation policy
for campaigns/escalations/proposals/sensor-sessions this pass never
audited under pytest.

SCOPE (module, not function -- revised after measuring, not guessing):
this repo's real jobs/ directory holds ~13,500 entries. The snapshot in
isolated_job_ledger_state() (job_ledger.list_all(), which reads and parses
every ledger.json) is fine done ONCE per file (the existing __main__-block
cost, already proven acceptable all session) but made two real files with
~30 test functions combined time out at 280s when first tried as a
function-scoped fixture -- a ~30x multiplier from doing that same snapshot
once per TEST FUNCTION instead of once per file. Module scope reproduces
the exact cost profile of the already-proven __main__ pattern (one
snapshot+teardown per file) while still closing the actual pytest-
invocation leak; it does not aim for finer granularity than the pattern it
is standing in for.

Safe to apply broadly (autouse, no opt-in marker needed): teardown
requires BOTH "new since this fixture's own snapshot" AND
is_test_isolation_fixture_job()'s exact provenance-marker match
(sandbox_path=="/tmp" AND requested_by in {"tester","test"} -- see
_test_isolation.py). No real production caller anywhere in this codebase
uses those values, so this fixture cannot terminalize a real job under any
circumstance -- including one the live autonomous-cycle timer creates
while a test module happens to be running -- regardless of how many
unrelated test functions it also wraps.
"""
from __future__ import annotations

import pytest

from _test_isolation import isolated_job_ledger_state


@pytest.fixture(scope="module", autouse=True)
def _pytest_isolated_job_ledger_state():
    with isolated_job_ledger_state():
        yield
