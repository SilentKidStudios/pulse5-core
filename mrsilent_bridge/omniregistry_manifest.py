"""OmniRegistry Manifest Census — Domain-A whole-Studio visibility gap-closure
(2026-09-08).

Real gap found and closed: the Studio's actual canonical division manifest
lives at /opt/pulse5-core/omniregistry/registry.json (82 real, named entries,
updated as recently as today) — a COMPLETELY SEPARATE, larger structure from
/opt/pulse5-core/omni_registry/ (862 files across 18 subdirectories, the
narrow, schema-fingerprinted source omni_registry_stewardship.py already
reads for Mission-intake eligibility — see that module's own 2026-08-24
survey docstring). organ_discovery.py's discover() already captures
omniregistry/'s full raw content (2579 files across 289 subdirectories) into
periodic read-only snapshots under discovery_data/, but nothing durable ever
aggregated registry.json's real entries into a Founder-facing answer — so
"what divisions exist Studio-wide" was structurally unanswerable from any
existing tested source before this module.

Deliberately VISIBILITY-ONLY, never intake: this module is read-only and
creates no Mission, campaign, or WorkItem, and is never called by
omni_registry_stewardship.py or any dispatch path — that module's own
carefully-verified schema fingerprint (sub_organs + true_godmode_requirements)
and intake semantics are completely unchanged. Whether THIS registry should
ever feed automatic Mission-intake is a separate, larger decision (different
schema; the other 288 omniregistry/ subdirectories sampled as
action_gates/approvals/autonomous_project_genesis/body_map-style state and
governance snapshots, matching the exact self-narrating sprawl pattern
omni_registry_stewardship.py's own survey already excluded from that other
registry) that deserves the same hands-on rigor, not a guess made here.

STATUS TAXONOMY, read directly from the real file, never invented:
  - type=="division", status=="active": a real, currently-operating Studio
    division.
  - type in ("project", "flagship_project"): real named project-level work.
  - type=="system": a named Studio subsystem.
  - type=="future_entity" (status=="concept", integration=="not_built"):
    explicitly NOT built — reported separately, NEVER counted as active or
    eligible work, and NEVER a candidate for auto-start. This category
    includes 'omnisim_expanded', directly adjacent to the protected
    OmniSim/Oracle Founder gate — reported as a bare fact, never acted on.
  - type=="discovered_signal" (status=="learned"): a recorded signal, not a
    division.
  - no 'type' key: an older schema variant present in the same registry
    file — reported as its own bucket rather than silently mis-typed.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

REGISTRY_MANIFEST_PATH = Path("/opt/pulse5-core/omniregistry/registry.json")


@dataclass
class ManifestCensus:
    source_path: str
    total_entries: int
    divisions_active: list[str]
    projects: list[str]
    systems: list[str]
    future_entities_not_built: list[str]
    discovered_signals_total: int
    legacy_untyped_total: int


def census() -> ManifestCensus | None:
    """Pure read; returns None (never raises, never fabricates a count) if
    the manifest file is absent or unparseable — callers must treat that as
    'no data available', never as zero divisions."""
    if not REGISTRY_MANIFEST_PATH.exists():
        return None
    try:
        data = json.loads(REGISTRY_MANIFEST_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(data, dict):
        return None

    divisions_active: list[str] = []
    projects: list[str] = []
    systems: list[str] = []
    future_entities: list[str] = []
    signals_total = 0
    legacy_total = 0

    for name, entry in data.items():
        if not isinstance(entry, dict):
            continue
        etype = entry.get("type")
        if etype is None:
            legacy_total += 1
        elif etype == "division" and entry.get("status") == "active":
            divisions_active.append(name)
        elif etype in ("project", "flagship_project"):
            projects.append(name)
        elif etype == "system":
            systems.append(name)
        elif etype == "future_entity":
            future_entities.append(name)
        elif etype == "discovered_signal":
            signals_total += 1

    return ManifestCensus(
        source_path=str(REGISTRY_MANIFEST_PATH),
        total_entries=len(data),
        divisions_active=sorted(divisions_active),
        projects=sorted(projects),
        systems=sorted(systems),
        future_entities_not_built=sorted(future_entities),
        discovered_signals_total=signals_total,
        legacy_untyped_total=legacy_total,
    )
