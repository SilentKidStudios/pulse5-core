from pathlib import Path
import json,sys,time
STATE=Path("/opt/pulse5-core/mr_silent_spine/founder_free_studio_runtime_v1/state")
STATE.mkdir(parents=True,exist_ok=True)

sys.path.insert(0, "/opt/pulse5-core/mr_silent_spine/walkaway_governance")
try:
    import walkaway_advance
    walkaway_report = walkaway_advance.run_cycle()
    walkaway_error = None
except Exception as e:
    walkaway_report = None
    walkaway_error = str(e)

summary={
 "time":time.strftime("%Y-%m-%dT%H:%M:%SZ",time.gmtime()),
 "module":"Founder-Free Studio Runtime Cycle",
 "status":"cycle_alive",
 "live_mutation_executed": bool(walkaway_report and walkaway_report.get("items_allowed", 0) > 0),
 "governance":"safe autonomous cycle active; risky mutation founder-gated",
 "walkaway_governance": {
    "auto_completion_authority": "NOT_IMPLEMENTED",
    "post_completion_auto_advance_and_delegation": "IMPLEMENTED_NARROW_SCOPE",
    "error": walkaway_error,
    "items_evaluated": walkaway_report.get("items_evaluated") if walkaway_report else None,
    "items_allowed": walkaway_report.get("items_allowed") if walkaway_report else None,
    "items_denied": walkaway_report.get("items_denied") if walkaway_report else None,
    "next_governing_priority": walkaway_report.get("next_governing_priority") if walkaway_report else None,
 },
}
(STATE/"latest_cycle.json").write_text(json.dumps(summary,indent=2))
print(json.dumps(summary,indent=2))
