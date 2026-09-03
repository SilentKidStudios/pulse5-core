# test_job_ledger_failure_normalization.py
# This test validates the mapping logic in job_ledger.classify_ledger_failure_transience.
# It uses a simple check helper and a FAILURES list to report failures.

import job_ledger

# Helper for simple assertions without external libraries.
FAILURES: list[str] = []

def check(name: str, condition: bool, detail: str="") -> None:
    """Append a failure message if condition is False.

    Args:
        name: A descriptive name for the check.
        condition: The boolean condition to test.
        detail: Optional additional detail.
    """
    if not condition:
        msg = f"{name} failed"
        if detail:
            msg += f": {detail}"
        FAILURES.append(msg)


def test_classify_ledger_failure_transience() -> None:
    """Test all expected mappings for classify_ledger_failure_transience."""
    cases = [
        ("infra", "timeout", "MODEL_TIMEOUT"),
        ("infra", "model_unavailable", "MODEL_UNAVAILABLE"),
        ("infra", "local_model_unavailable", "MODEL_UNAVAILABLE"),
        ("infra", "claude_unavailable", "MODEL_UNAVAILABLE"),
        ("infra", "error", "AMBIGUOUS"),
        ("infra", "iteration_ceiling_reached", "AMBIGUOUS"),
        ("model_escalate", "escalated", "TASK_FAILURE"),
        ("validation", "succeeded_validation_failed", "MODEL_FAILURE"),
        ("validator_disagreement", "succeeded_validator_disagreement", "MODEL_FAILURE"),
        ("authority", "rejected_policy", "AUTHORITY_BLOCK"),
        (None, None, "AMBIGUOUS"),
        ("totally_unknown", "totally_unknown", "AMBIGUOUS"),
    ]

    for error_class, terminal_result, expected in cases:
        result = job_ledger.classify_ledger_failure_transience(error_class, terminal_result)
        check(
            f"classify_ledger_failure_transience({error_class!r}, {terminal_result!r})",
            result == expected,
            f"expected {expected!r}, got {result!r}",
        )


if __name__ == "__main__":
    test_classify_ledger_failure_transience()
    print("FAILURES=" + str(FAILURES))
