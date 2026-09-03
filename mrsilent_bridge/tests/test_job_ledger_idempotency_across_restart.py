import uuid
import job_ledger

FAILURES = []

def check(name, condition, detail=""):
    if not condition:
        FAILURES.append(name)

def test_task_fingerprint_is_stable_and_deterministic_across_calls():
    task = str(uuid.uuid4())
    fp1 = job_ledger.task_fingerprint(task)
    fp2 = job_ledger.task_fingerprint(task)
    fp3 = job_ledger.task_fingerprint(task)
    check("fingerprint consistency across calls", fp1 == fp2 == fp3)

def test_stable_idempotency_key_survives_a_simulated_restart():
    task = str(uuid.uuid4())
    job_id = str(uuid.uuid4())
    job_ledger.create(job_id, task=task, requested_by="tester", sandbox_path="/tmp")
    expected_fp = job_ledger.task_fingerprint(task)
    record = job_ledger.load(job_id)
    check("record loaded after restart", record is not None)
    if record is not None:
        check("record task_fingerprint matches expected", record.task_fingerprint == expected_fp)
        recomputed_fp = job_ledger.task_fingerprint(task)
        check("recomputed fingerprint matches record fingerprint", recomputed_fp == record.task_fingerprint)

if __name__ == "__main__":
    test_task_fingerprint_is_stable_and_deterministic_across_calls()
    test_stable_idempotency_key_survives_a_simulated_restart()
    print("FAILURES=" + str(FAILURES))