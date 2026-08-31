# Independent Completion-Attestation Authority — Specification v1

**Status: specification only. Nothing in this document is implemented, deployed, or
executable. No code in this directory grants, performs, or simulates a canonical
completion transition.**

## 0. Why this component exists, and why it must not be an LLM

Two prior attempts in this session's history asked for the deterministic
completion verifier (`completion_verifier.py`) to also write
`founder_priority_state.json` itself. Both were declined, and independently, a
prior session (`walkaway_governance_campaign_receipt_20260831T202259Z.json`,
addendum `20260831T213543Z`) declined the same request before this session
existed. The reasoning in all three cases was the same: a component that both
(a) runs inside the same unattended, AI-operated pipeline that produces the
evidence it checks, and (b) has write access to canonical completion state, is
not independent of the thing it corroborates — no matter how deterministic its
individual checks are. Dispatching the *building* of that component to a
different AI adapter (Codex, Omni Engineer) does not change this: the produced
artifact is still authored, tested, and installed by the same AI-operated
pipeline, so the trust boundary is unchanged regardless of which model writes
the code (see this session's decline of the `task_router.route(..., founder_approved=True)`
dispatch request for the full reasoning, including the discovered gap that
`authority_policy.py`'s keyword/tool-based gate does not reliably catch a
plainly-worded engineering task).

This spec exists to define the one component whose introduction *would*
actually change that boundary: an **independent completion-attestation
authority** — deliberately **not** an LLM, with **no free-form reasoning
capability at all**. Its only job is to mechanically check a fixed list of
already-computed, already-deterministic preconditions and, if every one holds,
perform one narrowly-scoped write. It cannot originate a decision; it can only
confirm one that has already been made by deterministic checks and hand it to
the one existing write path that acts on it.

## 1. What is preserved, unchanged, by this design

- `mr_silent_spine/walkaway_governance/completion_contracts/*.json` — completion contracts, unchanged.
- `mr_silent_spine/walkaway_governance/completion_verifier.py` — the deterministic verifier, unchanged. Still never writes canonical state.
- OmniSim's `VERIFIED_COMPLETE` verdict (see §8) — unchanged, still not applied to canonical state.
- `mr_silent_spine/walkaway_governance/walkaway_advance.py` — the protected/default-deny classifier and post-completion advancement logic, unchanged.
- `/etc/systemd/system/founder-free-studio-runtime.path` (event wakeup) and `.timer` (15-min fallback) — unchanged.
- Worker routing/delegation (`safe_execution_router_v2b.py`, `studio_execution_layer/canonical_workers/`) — unchanged; this authority never touches it (§3).

## 2. Independent Attestation Schema

See `attestation_schema_v1.json` in this directory for the machine-readable
form. Summary of required fields and required values:

| field | required value / type |
|---|---|
| `attestation_version` | `"1.0"` |
| `priority_or_task_id` | string — the exact canonical item_id (`rank_id` or `rank_id/component_id`) |
| `completion_contract_id` | string — contract filename stem, e.g. `OMNISIM_AND_ORACLE_STUDIO_WIDE_ACTIVATION` |
| `completion_contract_version` | string, must equal the contract's own `completion_contract_version` |
| `canonical_classification` | **must equal `"NON_PROTECTED"`** |
| `verifier_verdict` | **must equal `"VERIFIED_COMPLETE"`** |
| `verifier_receipt_path` | string — path to the exact `completion_verifier.py` receipt consumed |
| `verifier_receipt_sha256` | string — sha256 of that exact receipt file, recomputed independently by the authority, not copied from the receipt itself |
| `evidence_set_hash` | string — sha256 of the receipt's `check_results` array, canonicalized (`json.dumps(..., sort_keys=True, separators=(',',':'))`) — binds the attestation to the *exact* evidence, not just the verdict string |
| `current_canonical_state` | string — the priority-state status the authority itself read at validation time (must be `pending` or `in_progress`) |
| `requested_transition` | **must equal `"complete"`** |
| `issued_at` | ISO-8601 UTC timestamp |
| `expires_at` | ISO-8601 UTC timestamp — short-lived; recommend `issued_at + 300s`. Forces re-validation against live state rather than acting on stale evidence. |
| `nonce` | random 128-bit value, hex-encoded — single-use |
| `idempotency_key` | `sha256(priority_or_task_id + "\|" + verifier_receipt_sha256 + "\|" + evidence_set_hash)` |

An attestation with any required-value field wrong, missing, or expired is
invalid and must be rejected without further evaluation — no partial credit,
no "close enough."

## 3. Minimum trust boundary — what the authority MUST NOT have

- No free-form AI/LLM reasoning of any kind. Its entire logic is the fixed
  validation sequence in §4 — a straight-line boolean chain, not a model call.
- No worker execution capability — it never invokes, selects, or spawns a
  worker, adapter, or engineering agent of any kind.
- No model-selection capability.
- No ability to modify, author, or backdate a completion contract.
- No ability to classify previously-unclassified work as `NON_PROTECTED` —
  classification is read-only input to it (§4 step 2), never something it
  computes or overrides.
- No protected-operation authority of any kind (production promotion,
  credential read/write, model deletion, Scorpio isolation, Render/GPU
  deletion) — these remain categorically outside its command surface, not
  merely denied by a runtime check.
- No credential-management authority.
- Write scope limited to exactly one field per invocation: the `status` of
  one named rank/component inside `founder_priority_state.json`, plus one
  append to the new audit ledger (§5). Nothing else on disk is reachable from
  its write path.

It may **only** validate the deterministic preconditions in §4 and, if all
hold, perform the bounded transition in §5.

## 4. Required independent validation sequence

Performed by the authority itself, against live state, at attestation-check
time — never trusting a field on the attestation object as self-proving:

1. **Contract existed before evaluation.** Read
   `mr_silent_spine/walkaway_governance/completion_contracts/<completion_contract_id>.json`
   from disk; confirm it exists, its `completion_contract_version` matches the
   attestation, and (belt-and-suspenders against a contract authored *after*
   the fact) its file mtime predates `issued_at`.
2. **Classifier currently says NON_PROTECTED.** Re-run
   `completion_verifier.classify_for_verification()` (or the equivalent
   read-only classification: `walkaway_advance.is_isolated` +
   `walkaway_advance.protected_gate_match` + the rank's `gate` field against
   `founder_priority_governor.RECOGNIZED_PROTECTED_GATES`) against the
   **current** `founder_priority_state.json` — not the attestation's claim.
   Must return `NON_PROTECTED`.
3. **Verifier verdict is VERIFIED_COMPLETE.** Re-read the receipt at
   `verifier_receipt_path` from disk (not from the attestation payload) and
   confirm its own `verdict` field is `VERIFIED_COMPLETE`.
4. **Verifier receipt hash matches.** Recompute sha256 of the receipt file on
   disk; must equal `verifier_receipt_sha256`.
5. **Evidence-set hash matches.** Recompute the canonicalized hash of that
   receipt's `check_results` array; must equal `evidence_set_hash`.
6. **Canonical state is still the expected pending/in-progress state.** Read
   `founder_priority_state.json` fresh (inside the lock, §5 step 2); the
   named item's `status` must still be `pending` or `in_progress` — not
   already `complete`, not `blocked`.
7. **Attestation has not expired.** `now_utc <= expires_at`.
8. **Nonce/idempotency key not already consumed.** Check both against a
   consumed-set store (e.g.
   `mr_silent_spine/walkaway_governance/independent_completion_authority/state/consumed_v1.json`)
   before proceeding, and record them there as part of the same atomic
   operation that performs the transition (§5) — not before, not after.
9. **No Founder-only gate applies.** Re-check the rank's `status != "blocked"`
   and `gate` field is empty/absent, independent of step 2 (belt-and-suspenders:
   step 2 already covers this, but a dedicated check makes the "Founder-only
   gate" requirement legible on its own rather than folded silently into
   classification).

Any single failure → reject, write nothing, do not consume the nonce.

## 5. Atomic canonical transition interface

```
verify_attestation()          # §4, all 9 checks
  → acquire_lock()             # exclusive, blocking or short-timeout-fail
  → recheck_state_and_classification()   # §4 steps 2 and 6, INSIDE the lock
  → atomic_write_complete()    # temp file + os.replace(), same directory
  → append_audit_record()      # append-only, same operation
  → consume_idempotency_key()  # same operation as the audit append
  → release_lock()
```

It must **not** directly launch, select, or notify a worker. The existing
`founder-free-studio-runtime.path` unit already watches
`founder_priority_state.json` for exactly this kind of change; the atomic
write in this step is itself the trigger. Everything downstream of the write —
event wakeup, governor continuation, work-item creation, worker routing,
delegation — belongs to the existing MR. SILENT runtime and must not be
duplicated here.

### Discovered canonical paths / interfaces

```
CANONICAL_PRIORITY_STATE_PATH        = mr_silent_spine/state/founder_priority_state.json
                                        (the "ranks" map; NEVER founder_top10_priority_queue.json,
                                         which is a documented read-cache projection, not source of truth)

CANONICAL_CLASSIFIER_PATH            = mr_silent_spine/walkaway_governance/walkaway_advance.py
                                        (is_isolated(), protected_gate_match(), ISOLATION_BOUNDARIES,
                                         PROTECTED_GATE_KEYWORDS)
                                        + mr_silent_spine/autonomous_exec/founder_priority_governor.py
                                        (RECOGNIZED_PROTECTED_GATES, for the rank "gate" field)

COMPLETION_VERIFIER_INTERFACE        = mr_silent_spine/walkaway_governance/completion_verifier.py
                                        :: verify_completion(item_id, cfg=None) -> dict
                                        (fields: task_id, contract_version, classification, checks_run,
                                         check_results, artifacts_verified, evidence_hashes, verdict,
                                         failure_reasons, timestamp_utc, validator_version)

COMPLETION_VERIFIER_RECEIPT_SCHEMA   = one JSON file per verification call, written to
                                        mr_silent_spine/walkaway_governance/verifier_receipts/
                                        <item_id_with_slashes_as_underscores>_<timestamp>.json,
                                        containing exactly the verify_completion() return dict

CANONICAL_LOCK_PATH_OR_STRATEGY      = does not exist yet. Required strategy: the authority creates
                                        and holds an exclusive flock (LOCK_EX) on
                                        mr_silent_spine/state/.founder_priority_state.lock
                                        for the entire duration of steps 2 (recheck) through
                                        5 (release) above — no other process may hold it
                                        concurrently. walkaway_advance.py and completion_verifier.py
                                        are read-only with respect to this file today and take no lock;
                                        this authority is the first and only writer, so introducing the
                                        lock here does not require changing either of them.

AUDIT_LEDGER_PATH                    = mr_silent_spine/walkaway_governance/ledger/completion_transitions_v1.jsonl
                                        (new; append-only; one record per transition:
                                         item_id, prior_state, resulting_state, contract_path,
                                         contract_version, verifier_receipt_path, verifier_receipt_sha256,
                                         evidence_set_hash, idempotency_key, nonce,
                                         authorization_basis="independent_completion_authority_v1",
                                         attested_by (the authority's own identity, §6), timestamp_utc)

EVENT_WAKEUP_PATH                    = /etc/systemd/system/founder-free-studio-runtime.path
                                        (PathModified=.../founder_priority_state.json)
                                        → founder-free-studio-runtime.service
                                        fallback: /etc/systemd/system/founder-free-studio-runtime.timer
                                        (OnBootSec=5min OnUnitActiveSec=15min) — both pre-existing,
                                        unmodified by any session to date

POST_COMPLETION_PROCESSOR_PATH       = mr_silent_spine/bin/founder_free_studio_runtime_cycle.py
                                        → mr_silent_spine/walkaway_governance/walkaway_advance.py
                                        :: run_cycle() — already idempotent, already does
                                        classification + evidence + work-item creation + worker-routing
                                        for any item now status=="complete"
```

## 6. Two safe implementation options (neither built in this session)

### Option A — external CI / attestation service with independent execution identity

- **Trust boundary:** Runs entirely outside this machine's AI-operated
  process tree — e.g. a scheduled job in an external CI system (GitHub
  Actions, a dedicated attestation runner) that only has read access to a
  mirror/export of the relevant files (contracts, verifier receipts,
  priority-state snapshot) and a narrow, audited write credential scoped to
  exactly one operation: pushing a signed "transition approved" record that a
  small, separately-deployed local writer applies. The AI pipeline can
  request an attestation but cannot itself grant one.
- **Required permissions:** Read-only pull of the four artifact classes in
  §4 (contract, receipt, priority-state, consumed-set); a single write
  credential limited to the attestation-result channel (e.g. a scoped repo
  deploy key restricted to one output path, or a signed webhook call) — no
  general filesystem or shell access to `/opt/pulse5-core`.
- **Secrets/credentials:** One signing key or deploy token, held only in the
  CI system's secret store, never on this machine, rotated by the Founder.
- **Failure behavior:** Any check failure or transport error → no
  attestation issued, no local write attempted; local writer treats "no
  attestation received" identically to "denied."
- **Audit behavior:** CI run log is itself an independent audit trail,
  separate from and cross-checkable against the local
  `completion_transitions_v1.jsonl`.
- **How protected work stays impossible:** the external service only ever
  sees `NON_PROTECTED` classification results as read-only input (§4 step 2
  is still evaluated fresh against live local state before the local writer
  acts, so a stale or spoofed "protected became non-protected" claim in
  transit cannot bypass the local recheck).
- **How ordinary eligible work stays autonomous:** once the Founder
  configures the CI job/credential once, every subsequent `NON_PROTECTED` +
  `VERIFIED_COMPLETE` item flows through without further Founder action.

### Option B — separately permissioned local non-LLM completion-attestation service

- **Trust boundary:** A small, dependency-light local process (e.g. a single
  Python script with no LLM/model dependency at all, per §3) running under
  its **own** restricted OS user/service account — not the account the
  AI-operated pipeline runs as — with filesystem permissions limited to:
  read on the contracts/receipts/priority-state paths in §5, and write only
  on `founder_priority_state.json`, the new lock file, and
  `completion_transitions_v1.jsonl`. No network access required.
- **Required permissions:** POSIX file ACLs or a dedicated systemd
  `DynamicUser=`/`ProtectSystem=strict` unit with an explicit
  `ReadWritePaths=` allowlist containing only those three paths.
- **Secrets/credentials:** none required — the trust boundary is OS-level
  file permissions, not a shared secret. (A signing key is optional
  hardening: the authority could sign its own audit records so a later
  reviewer can distinguish its writes from any other process's, without
  that key being usable to grant anything beyond signing.)
- **Failure behavior:** any §4 check failure → process exits non-zero,
  writes nothing, logs the specific failed predicate to its own log (not the
  audit ledger, which only records completed transitions and consumed
  attempts).
- **Audit behavior:** every invocation (pass or fail) is logged locally;
  only successful transitions are appended to
  `completion_transitions_v1.jsonl`.
- **How protected work stays impossible:** the service account itself has no
  write permission anywhere outside the three allowlisted paths — even a
  fully compromised or buggy build of this component cannot touch protected
  paths, credentials, or infrastructure, because the OS denies it, not
  because application logic remembers to check.
- **How ordinary eligible work stays autonomous:** the Founder sets up the
  service account, permissions, and unit file once; after that, any
  `NON_PROTECTED` + `VERIFIED_COMPLETE` item is transitioned without further
  Founder action, on whatever cadence the service is triggered (it can itself
  be invoked by the same `founder-free-studio-runtime.path`/`.timer` pair,
  or its own minimal path/timer unit watching the verifier's receipt
  directory — still not a second governor/scheduler, since it performs no
  scheduling decision of its own, only the fixed §4/§5 sequence).

Both options keep the same downstream contract: once
`founder_priority_state.json` changes, the **existing**, unmodified MR.
SILENT runtime (§1) does everything else.

## 7. Founder experience target

- **One-time setup:** Founder stands up Option A or B (service account,
  permissions, credential/signing key if any) — a deliberate, out-of-band
  action, not something granted via chat text to the AI-operated pipeline.
- **After that**, for any item with a valid, predeclared completion contract
  that reaches `NON_PROTECTED` + `VERIFIED_COMPLETE`: no manual completion
  marking, no per-task approval, no typing "continue," no manual worker
  selection, no manual tmux operation.
- **Protected work is unaffected** — it still requires Founder authorization
  exactly as it does today, because the independent authority has no
  capability to touch it (§3) and no capability to reclassify it (§4 step 2
  is always re-derived from the existing classifier, never the attestation's
  own claim).
