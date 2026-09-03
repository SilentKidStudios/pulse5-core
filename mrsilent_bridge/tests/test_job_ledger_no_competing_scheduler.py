import inspect
import job_ledger

FAILURES: list[str] = []

def check(name, condition, detail=""):
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def test_retry_contract_functions_introduce_no_competing_scheduler():
    """Ensure retry contract functions contain no competing scheduler primitives."""
    forbidden_markers = [
        "threading",
        "sched.",
        "asyncio",
        "time.sleep",
        "subprocess",
        "Timer(",
        "BackgroundScheduler",
        "cron",
    ]
    functions = [
        "is_transient_failure",
        "retry_ready_at",
        "should_stop_for_gate_decision",
        "find_active_by_fingerprint",
    ]

    for func_name in functions:
        func = getattr(job_ledger, func_name)
        source = inspect.getsource(func)
        for marker in forbidden_markers:
            check(f"{func_name} does not use {marker}", marker not in source, f"Found '{marker}' in source")


if __name__ == "__main__":
    test_retry_contract_functions_introduce_no_competing_scheduler()
    print("FAILURES=" + str(FAILURES))
