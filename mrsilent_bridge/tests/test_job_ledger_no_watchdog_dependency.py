# Test for is_retry_ready correctness at fine‑grained sub‑hourly intervals

from datetime import datetime, timedelta, timezone
import job_ledger

FAILURES: list[str] = []

def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        FAILURES.append(f"{name}: {detail}")


def test_retry_readiness_is_correct_at_fine_grained_sub_hourly_intervals() -> None:
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    failed_at = base.isoformat()
    ready_at = job_ledger.retry_ready_at(failed_at, attempt=1)
    offsets = [-30, -5, -1, 0, 1, 5, 30, 60]
    for offset in offsets:
        candidate_now = (base + timedelta(seconds=offset)).isoformat()
        expected = (base + timedelta(seconds=offset)) >= datetime.fromisoformat(ready_at)
        check(
            f"offset {offset}",
            job_ledger.is_retry_ready(ready_at, candidate_now) == expected,
            f"ready_at={ready_at}, candidate_now={candidate_now}"
        )


if __name__ == "__main__":
    test_retry_readiness_is_correct_at_fine_grained_sub_hourly_intervals()
    print("FAILURES=" + str(FAILURES))
