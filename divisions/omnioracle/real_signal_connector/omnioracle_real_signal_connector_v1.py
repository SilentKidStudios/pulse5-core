#!/usr/bin/env python3
"""Real signal connector blueprints. 3 of 5 connectors are now genuinely live:

  - SIG_SYSTEM_RUNTIME (2026-09-02, OMNI_ORACLE_GOD_MODE_V1_SYNTHETICITY_+_
    LIVE-SIGNAL_CLOSURE campaign): live_system_runtime_connector_v1.py reads real
    local machine telemetry (load average, memory, disk, this division's own
    systemd unit health), no new credentials.
  - SIG_APPROVAL_PATTERNS and SIG_STUDIO_ACTIVITY (2026-09-02,
    RESUME_OMNISIM_OMNI_ORACLE_FINAL_INTEGRATION campaign, after independently
    re-verifying the concurrent mr_silent_spine campaign's shared-scope collision
    was no longer current): live_approval_patterns_connector_v1.py reads
    mrsilent_bridge/job_ledger.py's real approval_state distribution;
    live_studio_activity_connector_v1.py reads
    mr_silent_spine/division_signal_bus/{inbox,receipts}/. Both are read-only
    against those trees -- neither writes anything there.

SIG_SOCIAL_TRENDS and SIG_MARKET_INTEL remain blueprint_ready/
live_collection_enabled=False: both need real paid external API credentials,
blocked outright without Founder authorization (PAID_AUTHORIZATION_REQUIRED)."""

import json, hashlib
from pathlib import Path
from datetime import datetime, timezone

ROOT=Path("/opt/pulse5-core")
ORACLE=ROOT/"divisions/omnioracle"

SIGNAL=ORACLE/"real_signal_connector"

CONNECTORS=SIGNAL/"connectors"
INBOX=SIGNAL/"inbox"
NORMALIZED=SIGNAL/"normalized"
STATE=SIGNAL/"state"

CONNECTORS.mkdir(parents=True, exist_ok=True)
INBOX.mkdir(parents=True, exist_ok=True)
NORMALIZED.mkdir(parents=True, exist_ok=True)
STATE.mkdir(parents=True, exist_ok=True)

def now():
    return datetime.now(timezone.utc).isoformat()

connector_blueprints=[
    {
        "connector_id":"SIG_SOCIAL_TRENDS",
        "source_type":"social_trends",
        "examples":["reddit","youtube","x","forums"],
        "status":"blueprint_ready",
        "live_collection_enabled":False
    },
    {
        "connector_id":"SIG_SYSTEM_RUNTIME",
        "source_type":"system_runtime",
        "examples":["cpu","memory","disk","service_health"],
        "status":"live",
        "live_collection_enabled":True,
        "implementation":"divisions/omnioracle/real_signal_connector/live_system_runtime_connector_v1.py",
        "note":"Flipped live 2026-09-02: local machine telemetry only, zero external credentials."
    },
    {
        "connector_id":"SIG_APPROVAL_PATTERNS",
        "source_type":"founder_behavior",
        "examples":["approvals","denials","priority_patterns"],
        "status":"live",
        "live_collection_enabled":True,
        "implementation":"divisions/omnioracle/real_signal_connector/live_approval_patterns_connector_v1.py",
        "note":"Flipped live 2026-09-02 (RESUME_OMNISIM_OMNI_ORACLE_FINAL_INTEGRATION): reads mrsilent_bridge/job_ledger.py's real approval_state distribution (6400+ real job records), read-only."
    },
    {
        "connector_id":"SIG_MARKET_INTEL",
        "source_type":"market_intelligence",
        "examples":["ai_tools","competitor_changes","industry_trends"],
        "status":"blueprint_ready",
        "live_collection_enabled":False
    },
    {
        "connector_id":"SIG_STUDIO_ACTIVITY",
        "source_type":"studio_internal_activity",
        "examples":["queue_growth","division_health","worker_output"],
        "status":"live",
        "live_collection_enabled":True,
        "implementation":"divisions/omnioracle/real_signal_connector/live_studio_activity_connector_v1.py",
        "note":"Flipped live 2026-09-02 (RESUME_OMNISIM_OMNI_ORACLE_FINAL_INTEGRATION): reads mr_silent_spine/division_signal_bus/{inbox,receipts}/, read-only."
    }
]

connector_files=[]

for conn in connector_blueprints:

    payload={
        **conn,
        "created_at":now(),
        "normalization_policy":{
            "requires_timestamp":True,
            "requires_source_id":True,
            "requires_confidence_score":True,
            "requires_risk_label":True
        },
        "future_mode":"continuous_signal_ingestion"
    }

    hid=hashlib.sha256(
        json.dumps(payload,sort_keys=True).encode()
    ).hexdigest()[:16]

    fp=CONNECTORS/f"{conn['connector_id']}_{hid}.json"
    fp.write_text(json.dumps(payload,indent=2))

    connector_files.append(str(fp))

normalization_schema={
    "schema":"omnioracle_signal_normalization_v1",
    "required_fields":[
        "signal_id",
        "source_type",
        "timestamp",
        "confidence",
        "importance",
        "risk_level",
        "raw_payload"
    ],
    "status":"ready"
}

schema_file=NORMALIZED/"signal_normalization_schema_v1.json"
schema_file.write_text(json.dumps(normalization_schema,indent=2))

state={
    "updated_at":now(),
    "engine":"omnioracle_real_signal_connector_v1",
    "connector_count":len(connector_files),
    "connector_files":connector_files,
    "normalization_schema":str(schema_file),
    "real_time_ingestion_active":False,
    "live_execution_performed":False,
    "production_overwrite_performed":False,
    "status":"real_signal_connector_foundation_ready"
}

state_file=STATE/"omnioracle_real_signal_connector_v1_state.json"
state_file.write_text(json.dumps(state,indent=2))

print(json.dumps(state,indent=2))
