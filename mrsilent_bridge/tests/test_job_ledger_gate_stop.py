import job_ledger

FAILURES = []

def check(name, condition, detail=""):
    if not condition:
        if detail:
            FAILURES.append(f"{name}: {detail}")
        else:
            FAILURES.append(name)

def test_authority_denied():
    result = job_ledger.should_stop_for_gate_decision(risk_class="low", approval_state="not_required", authority_state="denied")
    check("test_authority_denied", result is True, f"Expected True, got {result}")

def test_approval_rejected():
    result = job_ledger.should_stop_for_gate_decision(risk_class="medium", approval_state="rejected", authority_state="granted")
    check("test_approval_rejected", result is True, f"Expected True, got {result}")

def test_founder_gated_pending():
    result = job_ledger.should_stop_for_gate_decision(risk_class="founder_gated", approval_state="pending_approval", authority_state="granted")
    check("test_founder_gated_pending", result is True, f"Expected True, got {result}")

def test_founder_gated_granted():
    result = job_ledger.should_stop_for_gate_decision(risk_class="founder_gated", approval_state="granted", authority_state="granted")
    check("test_founder_gated_granted", result is False, f"Expected False, got {result}")

def test_low_not_required_granted():
    result = job_ledger.should_stop_for_gate_decision(risk_class="low", approval_state="not_required", authority_state="granted")
    check("test_low_not_required_granted", result is False, f"Expected False, got {result}")

def test_none_all():
    result = job_ledger.should_stop_for_gate_decision(risk_class=None, approval_state=None, authority_state=None)
    check("test_none_all", result is False, f"Expected False, got {result}")

if __name__ == "__main__":
    test_authority_denied()
    test_approval_rejected()
    test_founder_gated_pending()
    test_founder_gated_granted()
    test_low_not_required_granted()
    test_none_all()
    print("FAILURES=" + str(FAILURES))