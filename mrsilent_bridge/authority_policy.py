"""
Authority policy for the MR. SILENT Claude Engineer Bridge.

Doctrine: UNLIMITED OUTSIDE CAPABILITY, EXTREMELY LIMITED OUTSIDE AUTHORITY.

This module is the single place that decides whether a requested job is allowed
to run unattended (LOW risk) or must stop and wait for an explicit, human-set
`founder_approved=True` (FOUNDER_GATED). It is deliberately conservative: unknown
tools, unknown paths, and unknown risk indicators default to gated, not allowed.

Nothing in this file can grant itself more authority — GATED_TOOLS,
GATED_PATH_MARKERS and GATED_KEYWORDS are read at call time, not mutated by
job code.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

import secret_path_policy


class RiskClass(str, Enum):
    LOW = "low"                 # inspect, analyze, propose, backup, sandboxed edit, tests, static validation
    MEDIUM = "medium"           # broader sandboxed changes, still contained, still reviewable pre-promotion
    FOUNDER_GATED = "founder_gated"  # deletion, credentials, paid services, prod-wide change, OS change, security boundary


class ApprovalState(str, Enum):
    NOT_REQUIRED = "not_required"
    PENDING = "pending_approval"
    GRANTED = "granted"          # only ever set by a human passing founder_approved=True at call time
    REJECTED = "rejected"


# Tools that immediately escalate a job to FOUNDER_GATED if requested.
# Bash is explicitly excluded from the default allowlist: "no arbitrary shell authority".
GATED_TOOLS = frozenset({
    "Bash", "WebFetch", "WebSearch", "Agent", "NotebookEdit",
})

# Tools allowed to run unattended at LOW risk, provided the job's working
# directory stays inside the sandbox (see bridge.py path jail).
DEFAULT_LOW_RISK_TOOLS = frozenset({"Read", "Grep", "Glob", "Edit", "Write"})

# Adapters/providers that are ALWAYS founder-gated, regardless of task content
# or requested tools — because the provider itself, not just what it's asked
# to do, is the risk. Codex is a paid external service (billed against the
# machine's authenticated ChatGPT account) whose only writable sandbox mode
# (workspace-write) grants shell command execution with no per-tool allowlist
# the way Claude Code's --allowedTools does — i.e. "arbitrary shell authority"
# is inherent to using it at all, not something a task can opt out of.
GATED_ADAPTERS = frozenset({"codex"})

# Absolute path fragments that make a job FOUNDER_GATED regardless of tools,
# because they name protected / production / credential-bearing / other-studio areas.
# Extended 2026-09-02 (SECRET / CREDENTIAL SOURCE-STAGING HARDENING campaign)
# with secret_path_policy.NEW_CONFIRMED_SECRET_MARKERS -- see that module's
# docstring for the read-only audit these were found from (secure_keys/,
# secrets/, secure/, oauth_state/, founder_session_bridge/,
# social_cookie_vault/, .config/doctl/, client_secret.json,
# ops_console_token.txt, mr_silent_auth.json).
GATED_PATH_MARKERS = (
    "scorpios-corner-voice",
    "SCORPIOS_CORNER",
    "D3C3H6F_REFERENCE_LOCK",
    "elevenlabs",
    "omnisonus",
    "pulse-api",
    "containerd",
    "/.ssh",
    "/.aws",
    "/.config/gcloud",
    "credentials",
    "secrets",
    ".env",
) + secret_path_policy.NEW_CONFIRMED_SECRET_MARKERS

# Task-description keywords that force FOUNDER_GATED even if tools/paths look fine,
# because the *intent* described is inherently high-authority.
GATED_KEYWORDS = tuple(re.compile(p, re.IGNORECASE) for p in (
    r"\bdelete\b", r"\brm -rf\b", r"\bdrop table\b", r"\btruncate\b",
    r"\bapt (upgrade|dist-upgrade)\b", r"\breboot\b", r"\bshutdown\b",
    r"\bsystemctl (restart|stop)\b",
    r"\bcredential", r"\bsecret", r"\bapi[_ ]?key\b", r"\btoken\b",
    r"\bpayment\b", r"\bmoney\b", r"\bbilling\b", r"\bcharge\b",
    r"\belevenlabs\b", r"\bscorpio", r"\bprotected model",
    r"\bproduction[- ]wide\b", r"\bmigrat(e|ion)\b",
    r"\bpermission(s)? (boundary|policy)\b", r"\bauth\b.*\b(bypass|disable)\b",
))


@dataclass
class PolicyDecision:
    risk_class: RiskClass
    approval_state: ApprovalState
    reasons: list[str] = field(default_factory=list)
    granted_tools: frozenset[str] = field(default_factory=frozenset)

    @property
    def may_execute(self) -> bool:
        return self.approval_state in (ApprovalState.NOT_REQUIRED, ApprovalState.GRANTED)


def classify(
    task_description: str,
    requested_tools: set[str],
    sandbox_root: Path,
    source_paths: list[Path] | None = None,
    founder_approved: bool = False,
    adapter: str | None = None,
) -> PolicyDecision:
    """Classify a job request and decide whether it may run unattended.

    `sandbox_root` is the job's isolated working directory. `source_paths` are any
    real filesystem paths the job asked to copy *into* the sandbox for editing
    (see bridge.py) — these are checked against GATED_PATH_MARKERS even though the
    job itself only ever touches the sandbox copy, because the *intent* to touch a
    protected area is itself the thing being gated. `adapter` names which engine
    would run the job (e.g. "claude_code", "codex") — adapters in GATED_ADAPTERS
    are unconditionally founder-gated regardless of task content or tools.
    """
    reasons: list[str] = []
    risk = RiskClass.LOW

    if adapter in GATED_ADAPTERS:
        risk = RiskClass.FOUNDER_GATED
        reasons.append(f"adapter '{adapter}' is always founder-gated (paid external service / no per-tool sandbox)")

    unknown_tools = set(requested_tools) - DEFAULT_LOW_RISK_TOOLS
    gated_requested = requested_tools & GATED_TOOLS
    if gated_requested:
        risk = RiskClass.FOUNDER_GATED
        reasons.append(f"requested gated tool(s): {sorted(gated_requested)}")
    elif unknown_tools:
        risk = RiskClass.MEDIUM
        reasons.append(f"requested tool(s) outside default low-risk set: {sorted(unknown_tools)}")

    for p in (source_paths or []):
        p_str = str(p)
        # Resolve BEFORE marker/boundary checks (2026-09-02 hardening): a raw
        # caller string can hide a marker or escape the repo entirely behind
        # '..' segments or a symlink (e.g. .env.central -> /etc/pulse5.env);
        # checking the RESOLVED path closes that bypass. resolve() never
        # raises for a nonexistent path.
        resolved = secret_path_policy.resolve_path(p)

        if not secret_path_policy.is_within_root(resolved):
            risk = RiskClass.FOUNDER_GATED
            reasons.append(
                f"source path resolves outside the canonical repository root "
                f"({secret_path_policy.ROOT}): {p_str} -> {resolved}"
            )
            continue  # already gated; no need to also marker-scan an out-of-root path

        for marker in GATED_PATH_MARKERS:
            if marker.lower() in str(resolved).lower():
                risk = RiskClass.FOUNDER_GATED
                reasons.append(f"source path touches protected marker '{marker}': {p_str} -> {resolved}")

    sandbox_str = str(sandbox_root.resolve())
    bridge_root = str(Path(__file__).resolve().parent)
    if sandbox_str == bridge_root or not sandbox_str.startswith(bridge_root):
        risk = RiskClass.FOUNDER_GATED
        reasons.append(
            "sandbox_root is not a subdirectory of the bridge's own jobs/ tree "
            "(self-modification / out-of-jail path)"
        )

    # V10.3R3 real-incident fix (Founder-authorized 2026-08-26,
    # MULTIMODAL_PROVIDER_REJECTED / EXACT_FAILURE_CLASS investigation):
    # physical evidence (job e2aa6269-9909-4285-be8b-785bf19276e3, a real
    # S25 Gallery image) proved this scan was firing on the CONTENT being
    # analyzed rather than an engineering instruction. local_model_bridge.
    # analyze_image()/local_video_bridge.analyze_video()'s proven two-stage
    # caption-then-reason pattern passes the model's own real image
    # description straight into the REASONING pass's task_description (by
    # design — see analyze_image()'s docstring on why "see+reason" in one
    # pass is worse). That description's ordinary mention of "payment" /
    # "charge" (real ad copy: "an advertisement for SeeDance 2.5...")
    # matched \bpayment\b / \bcharge\b and FOUNDER_GATED the whole request
    # — exactly the same way for ANY image whose real visible content uses
    # one of GATED_KEYWORDS' ~20 English words, with zero relation to
    # whether the Studio could ever actually do anything dangerous: this
    # call site (adapter="local_model", zero requested_tools, zero
    # source_paths — see submit_job()'s own "no tools: this adapter never
    # grants file/shell access to the model" docstring) is structurally
    # incapable of acting regardless of what classify() decides here, so
    # keyword-gating its prompt text protects nothing and only produces
    # false positives on ordinary analyzed content. This exception is
    # scoped to that exact, provably actionless call shape — every other
    # adapter (codex, the omni_engineer harness, the generic bridge.py
    # engineering path) keeps the identical unconditional keyword scan.
    skip_content_keyword_scan = (
        adapter == "local_model" and not requested_tools and not (source_paths or [])
    )
    for pattern in GATED_KEYWORDS:
        if not skip_content_keyword_scan and pattern.search(task_description or ""):
            risk = RiskClass.FOUNDER_GATED
            reasons.append(f"task description matched gated keyword pattern: {pattern.pattern!r}")

    if risk == RiskClass.FOUNDER_GATED:
        approval = ApprovalState.GRANTED if founder_approved else ApprovalState.PENDING
        if not founder_approved:
            reasons.append("FOUNDER_GATED and founder_approved=False: job will be recorded but not executed")
    else:
        approval = ApprovalState.NOT_REQUIRED

    granted = frozenset(requested_tools) if risk != RiskClass.FOUNDER_GATED or founder_approved else frozenset()

    return PolicyDecision(risk_class=risk, approval_state=approval, reasons=reasons, granted_tools=granted)
