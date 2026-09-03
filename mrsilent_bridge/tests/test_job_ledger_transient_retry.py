import unittest
from datetime import datetime, timezone, timedelta

from job_ledger import is_transient_failure, retry_ready_at

class TestJobLedgerTransientRetry(unittest.TestCase):

    def test_retry_interval_60_to_120_seconds(self):
        base_ts = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        attempts = [1, 30, 61, 62, 100]
        for attempt in attempts:
            with self.subTest(attempt=attempt):
                new_ts = retry_ready_at(base_ts, attempt)
                base_dt = datetime.fromisoformat(base_ts)
                new_dt = datetime.fromisoformat(new_ts)
                delta = (new_dt - base_dt).total_seconds()
                self.assertGreaterEqual(delta, 60, f"Attempt {attempt} yielded delta {delta} < 60")
                self.assertLessEqual(delta, 120, f"Attempt {attempt} yielded delta {delta} > 120")

    def test_transient_failure_retries(self):
        self.assertTrue(is_transient_failure("timeout"))
        self.assertTrue(is_transient_failure("unavailable"))
        self.assertTrue(is_transient_failure("connection_error"))
        self.assertTrue(is_transient_failure("rate_limited"))

    def test_non_transient_failure_fails_closed(self):
        self.assertFalse(is_transient_failure("some_unlisted_error"))
        self.assertFalse(is_transient_failure(""))
        self.assertFalse(is_transient_failure("UNKNOWN"))

if __name__ == "__main__":
    unittest.main()
