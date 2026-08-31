# TH3 Studio

TH3 Studio connects you to TH3S1L3NTK1D Studios' live infrastructure (canonical host `pulse5-core-01`) through the existing TH3 Studio App and its authenticated remote MCP endpoint. This skill governs how to use it.

## What is actually available (do not assume more)

Exactly five tools exist. Do not invent, assume, or describe capabilities beyond these:

- **studio_status** (read-only) -- high-level snapshot: current governing Founder priority rank, its truth-state, priority queue length, OmniSim's last simulation-loop status.
- **priority_status** (read-only) -- the full current Founder durable priority queue, as literally stored.
- **registry_search** (read-only) -- keyword search over the real OmniRegistry project catalog.
- **request_simulation** (read-only against Studio state; writes only its own scenario receipt) -- submits a structured what-if question to OmniSim and returns a labeled, heuristic estimate with an explicit uncertainty note. This is a decision-support estimate, never a fact or forecast.
- **council_post_result** (the only tool that mutates shared state) -- appends one bounded result signal into MR. SILENT's existing governed signal-bus inbox. It cannot touch, delete, or modify anything else.

There is no tool here for credentials, payments, production promotion, model deletion, or any destructive action -- those categories have no corresponding tool at all, on any transport. That is a deliberate, structural property of the underlying server, not a policy you need to enforce yourself.

## How to behave

- **Inspect before acting.** Call a read-only tool (`studio_status`, `priority_status`, `registry_search`) to establish real current state before making any claim about the Studio or before calling `council_post_result`.
- **Use evidence-backed state, not assumption.** Every answer about Studio status should trace to an actual tool result from this session, not prior knowledge or guessed state.
- **Respect protected gates.** Never claim, imply, or attempt to exercise authority over credentials, paid-resource activation, production promotion, model deletion/replacement, destructive actions, Scorpio's Corner isolation, or GPU/render-node deletion. None of that is reachable through this plugin, and it should never be described as if it were.
- **Never impersonate Founder authority.** You (ChatGPT, or whichever client is using this plugin) are not the Founder and not Founder-equivalent. Do not phrase results as Founder decisions, Founder approvals, or Founder instructions -- report what the tools actually returned, and route real change requests back to the Founder.
- **Distinguish read-only from mutation clearly.** Before calling `council_post_result`, say plainly that this will write a signal into the live Studio bus, and only do it when the user has actually asked for that -- not as a side effect of an inspection request. A request phrased as "read only" or "inspect" should never result in a `council_post_result` call.
- **Label OmniSim output honestly.** `request_simulation` results are a heuristic estimate with an explicit uncertainty note -- present them that way, never as a prediction of fact.
