"""OmniEngineer adapter — wired to omniengineer_harness.py, the bounded local
coding-agent loop built around qwen3-coder:30b (see that module's docstring).
Unlike local_model_adapter.py (analysis/text-completion only), this grants a
sandboxed file-editing tool loop — but only ever inside one job's own
workdir, never against real repository files (V0.1 has no source_paths
support, see omniengineer_harness.py's module docstring)."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

import omniengineer_harness


def run(task: dict[str, Any]) -> dict[str, Any]:
    # ROUTER_INTEGRATION (Phase 3): submit_job_auto() classifies complexity
    # inside the Omni capability boundary and dispatches to submit_job() or
    # submit_job_decomposed() accordingly -- task_router.py itself is
    # unchanged, still routes here for every omni_engineer task exactly as
    # before.
    result = omniengineer_harness.submit_job_auto(
        task=task["description"],
        requested_by=task.get("requested_by", "task_router"),
        model=task.get("model", omniengineer_harness.DEFAULT_MODEL),
        timeout_s=task.get("timeout_s", omniengineer_harness.DEFAULT_TIMEOUT_S),
        founder_approved=task.get("founder_approved", False),
        max_iterations=task.get("max_iterations", omniengineer_harness.MAX_ITERATIONS),
        validation_config=task.get("validation_config"),
    )
    return asdict(result)
