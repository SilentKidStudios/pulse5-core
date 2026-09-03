# Test for extended transient failure detection

FAILURES = []

def check(name, condition, detail=""):
    if not condition:
        FAILURES.append(f"{name}: {detail or 'FAILED'}")

import job_ledger


def test_is_transient_failure_extended():
    tests = {
        "timeout": True,
        "unavailable": True,
        "connection_error": True,
        "rate_limited": True,
        "MODEL_TIMEOUT": True,
        "MODEL_UNAVAILABLE": True,
        "PROVIDER_TIMEOUT": True,
        "PROVIDER_UNAVAILABLE": True,
        "PROVIDER_FAILURE": True,
        "AMBIGUOUS": False,
        "TASK_FAILURE": False,
        "MODEL_FAILURE": False,
        "AUTHORITY_BLOCK": False,
        "something_else": False,
    }
    for err, expected in tests.items():
        result = job_ledger.is_transient_failure(err)
        check(f"is_transient_failure('{err}')", result == expected,
              f"expected {expected} got {result}")


if __name__ == "__main__":
    test_is_transient_failure_extended()
    print("FAILURES=" + str(FAILURES))