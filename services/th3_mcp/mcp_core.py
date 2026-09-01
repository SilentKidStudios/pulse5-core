#!/usr/bin/env python3
"""
Shared MCP tool registry and JSON-RPC dispatch logic, used by BOTH the stdio
transport (server.py) and the HTTP transport (http_server.py) so the tool set
cannot drift between them -- BLEND_NOT_REPLACE, single source of truth.

This module has no transport-specific code (no stdin/stdout, no HTTP). It only
knows how to turn one JSON-RPC message into one JSON-RPC response dict.
"""

from __future__ import annotations

import json
from typing import Any

import tools as T

SERVER_NAME = "th3s1l3ntk1d-studios-mcp"
SERVER_VERSION = "0.1.0"

TOOL_SPECS = [
    {
        "name": "studio_status",
        "description": "High-level real-time snapshot of TH3S1L3NTK1D Studios: current governing Founder priority rank, its truth-state, priority queue length, and OmniSim's last simulation loop status. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "fn": lambda args: T.studio_status(),
    },
    {
        "name": "priority_status",
        "description": "Full current Founder durable priority queue (all ranks, IDs, and explicit truth-state), as literally stored in canonical state. Read-only.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "fn": lambda args: T.priority_status(),
    },
    {
        "name": "registry_search",
        "description": "Keyword search over the real OmniRegistry truth-layer project catalog. Read-only.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "keyword(s) to search for"},
                "limit": {"type": "integer", "description": "max results", "default": 10},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
        "fn": lambda args: T.registry_search(args.get("query", ""), int(args.get("limit", 10))),
    },
    {
        "name": "request_simulation",
        "description": "Submit a structured scenario question to OmniSim (governed simulation organ) and receive assumptions, scored options, an explicit uncertainty note, and recommendation boundaries. This is a labeled estimate, never presented as fact.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "question": {"type": "string"},
                "assumptions": {"type": "array", "items": {"type": "string"}},
                "options": {"type": "array", "items": {"type": "string"}, "description": "candidate options to score; defaults to execute_now/ask_founder/simulate_more/hold"},
            },
            "required": ["question"],
            "additionalProperties": False,
        },
        "fn": lambda args: T.request_simulation(args.get("question", ""), args.get("assumptions"), args.get("options")),
    },
    {
        "name": "council_post_result",
        "description": "Post a bounded result back into the Studio's existing governed division-signal-bus inbox (source_division defaults to CT_MCP_BRIDGE). This is the ONLY write-capable tool; it can only append one schema-matching signal file, nothing else.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_label": {"type": "string", "default": "CT_MCP_BRIDGE"},
                "in_reply_to": {"type": "string", "description": "id of the request this answers"},
                "summary": {"type": "string"},
                "payload": {"type": "object"},
            },
            "required": ["in_reply_to", "summary", "payload"],
            "additionalProperties": False,
        },
        "fn": lambda args: T.council_post_result(
            args.get("source_label", "CT_MCP_BRIDGE"),
            args.get("in_reply_to", ""),
            args.get("summary", ""),
            args.get("payload", {}),
        ),
    },
    {
        "name": "work_status",
        "description": "Read-only lookup of one canonical work item by id (checks the MR. SILENT engineering job ledger first, then Founder Top-10 priority ranks). Returns found=false rather than fabricating a result.",
        "inputSchema": {
            "type": "object",
            "properties": {"work_id": {"type": "string", "maxLength": 200}},
            "required": ["work_id"],
            "additionalProperties": False,
        },
        "fn": lambda args: T.work_status(args.get("work_id", "")),
    },
    {
        "name": "active_work",
        "description": "Read-only bounded list of canonical non-terminal work: engineering jobs still in flight in the job ledger, and Founder Top-10 priority ranks not yet complete.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "max engineering jobs to return (1-100)", "default": 20}},
            "additionalProperties": False,
        },
        "fn": lambda args: T.active_work(int(args.get("limit", 20))),
    },
    {
        "name": "route_preview",
        "description": "PURE DRY RUN. Shows which registered Studio capability would be proposed for a task_type, and whether the task description/tools would trip Founder-gated authority indicators. Creates no job, queue entry, execution, or approval item.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_type": {"type": "string", "maxLength": 200},
                "task_description": {"type": "string", "maxLength": 4000, "default": ""},
                "requested_tools": {"type": "array", "items": {"type": "string"}, "default": []},
            },
            "required": ["task_type"],
            "additionalProperties": False,
        },
        "fn": lambda args: T.route_preview(
            args.get("task_type", ""), args.get("task_description", ""), args.get("requested_tools", []),
        ),
    },
    {
        "name": "capability_status",
        "description": "Read-only inventory of the real Studio capability registry: which brains/providers are actually invocable vs. discovery-only, and their health/availability/cost/risk class.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "fn": lambda args: T.capability_status(),
    },
    {
        "name": "runtime_health",
        "description": "Read-only compact health of the persistent MR. SILENT runtime (a fixed, hardcoded set of systemd units -- no arbitrary systemctl capability) and the engineering job queue's non-terminal/stale counts.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "fn": lambda args: T.runtime_health(),
    },
    {
        "name": "continuum_status",
        "description": "Read-only CT-facing aggregate: governing Founder priority, active work summary, latest completed job, pending Founder-gated jobs, runtime health, and next actionable ordinary work -- composed entirely from existing canonical sources, no new truth store.",
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
        "fn": lambda args: T.continuum_status(),
    },
    {
        "name": "submit_work",
        "description": "Submit a structured Studio objective. Creates exactly one Proposal in the existing self-evolution pipeline if it can be safely classified (default-deny otherwise). Never executes anything synchronously -- only the existing, already-Founder-authorized 15-minute autonomous-cycle timer ever advances it, and only if classified low-risk, capped at PROMOTION_CANDIDATE (never auto-promoted to real files). No founder_approved override exists here.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "maxLength": 4000},
                "task_type": {"type": "string", "maxLength": 100, "default": ""},
                "context": {"type": "string", "maxLength": 4000, "default": ""},
                "priority_hint": {"type": "string", "maxLength": 50, "default": ""},
                "requested_capabilities": {"type": "array", "items": {"type": "string", "maxLength": 100}, "maxItems": 20, "default": []},
                "idempotency_key": {"type": "string", "maxLength": 200},
            },
            "required": ["objective"],
            "additionalProperties": False,
        },
        "fn": lambda args: T.submit_work(
            args.get("objective", ""), args.get("task_type", ""), args.get("context", ""),
            args.get("priority_hint", ""), args.get("requested_capabilities", []), args.get("idempotency_key"),
        ),
    },
    {
        "name": "work_result",
        "description": "Read-only. Current lifecycle state of a submit_work item: objective, status, classification, latest implementation attempt (engine, validation/canary result) if any, approval state, and history. Never fabricates a result that isn't on disk.",
        "inputSchema": {
            "type": "object",
            "properties": {"work_id": {"type": "string", "maxLength": 200}},
            "required": ["work_id"],
            "additionalProperties": False,
        },
        "fn": lambda args: T.work_result(args.get("work_id", "")),
    },
    {
        "name": "request_founder_decision",
        "description": "Surfaces an EXISTING Founder-gated decision point on a submit_work item in structured form (why approval is required, what decision is being asked for). Creates no second approval system and never auto-approves; read-only against the real proposal state.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "work_id": {"type": "string", "maxLength": 200},
                "reason": {"type": "string", "maxLength": 1000, "default": ""},
            },
            "required": ["work_id"],
            "additionalProperties": False,
        },
        "fn": lambda args: T.request_founder_decision(args.get("work_id", ""), args.get("reason", "")),
    },
    {
        "name": "cancel_work",
        "description": "Governed, idempotent cancellation -- ONLY for a submit_work item with zero implementation attempts yet (moves it to REJECTED via the existing proposal lifecycle). Reports unsupported, rather than inventing a mechanism, once an implementation attempt has started -- there is no canonical process-kill path in this project.",
        "inputSchema": {
            "type": "object",
            "properties": {"work_id": {"type": "string", "maxLength": 200}},
            "required": ["work_id"],
            "additionalProperties": False,
        },
        "fn": lambda args: T.cancel_work(args.get("work_id", "")),
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOL_SPECS}


def _result(req_id, result) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def _error(req_id, code, message) -> dict:
    return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}


def handle_request(msg: dict) -> dict | None:
    """Pure function: one JSON-RPC request dict in, one response dict out
    (or None for a notification, which gets no response)."""
    method = msg.get("method")
    req_id = msg.get("id")
    params = msg.get("params") or {}

    if method == "initialize":
        return _result(req_id, {
            "protocolVersion": "2024-11-05",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
        })

    if method == "notifications/initialized":
        return None

    if method == "tools/list":
        return _result(req_id, {
            "tools": [
                {"name": t["name"], "description": t["description"], "inputSchema": t["inputSchema"]}
                for t in TOOL_SPECS
            ]
        })

    if method == "tools/call":
        name = params.get("name")
        args = params.get("arguments") or {}
        tool = TOOLS_BY_NAME.get(name)
        if tool is None:
            return _error(req_id, -32601, f"unknown tool: {name}")
        try:
            out = tool["fn"](args)
            return _result(req_id, {"content": [{"type": "text", "text": json.dumps(out, indent=2)}], "isError": False})
        except Exception as exc:
            return _result(req_id, {
                "content": [{"type": "text", "text": f"tool error: {exc}"}],
                "isError": True,
            })

    if req_id is not None:
        return _error(req_id, -32601, f"unknown method: {method}")
    return None
