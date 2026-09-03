import uuid
import job_ledger
from _test_isolation import isolated_test_state  # root-cause test-isolation guarantee, see tests/_test_isolation.py

# simple test helper
FAILURES = []

def check(name, condition, detail=""):
    if not condition:
        FAILURES.append(name)


def test_active_work_precheck_finds_a_still_active_job_by_fingerprint():
    job_id = str(uuid.uuid4())
    task = str(uuid.uuid4())
    # create a new job record
    job_ledger.create(
        job_id,
        task=task,
        requested_by="tester",
        sandbox_path="/tmp",
    )
    # move it to a non‑terminal state
    job_ledger.checkpoint(job_id, job_ledger.JobState.EDITING)
    # find active record by fingerprint
    rec = job_ledger.find_active_by_fingerprint(job_ledger.task_fingerprint(task))
    check("test1_found_record", rec is not None)
    check("test1_job_id_matches", rec is not None and rec.job_id == job_id)


def test_a_terminal_completed_job_does_not_block_a_new_legitimate_request():
    job_id = str(uuid.uuid4())
    task = str(uuid.uuid4())
    job_ledger.create(
        job_id,
        task=task,
        requested_by="tester",
        sandbox_path="/tmp",
    )
    # mark as completed
    job_ledger.checkpoint(job_id, job_ledger.JobState.COMPLETED, terminal_result="succeeded")
    rec = job_ledger.find_active_by_fingerprint(job_ledger.task_fingerprint(task))
    check("test2_rec_none", rec is None)


def test_an_unrecognized_state_value_still_counts_as_active_fail_closed():
    job_id = str(uuid.uuid4())
    task = str(uuid.uuid4())
    job_ledger.create(
        job_id,
        task=task,
        requested_by="tester",
        sandbox_path="/tmp",
    )
    # set an unrecognized state string
    job_ledger.checkpoint(job_id, "some_totally_unrecognized_state_xyz")
    rec = job_ledger.find_active_by_fingerprint(job_ledger.task_fingerprint(task))
    check("test3_found_record", rec is not None)
    check("test3_job_id_matches", rec is not None and rec.job_id == job_id)


if __name__ == "__main__":
    with isolated_test_state():
        test_active_work_precheck_finds_a_still_active_job_by_fingerprint()
        test_a_terminal_completed_job_does_not_block_a_new_legitimate_request()
        test_an_unrecognized_state_value_still_counts_as_active_fail_closed()
    print("FAILURES=" + str(FAILURES))
