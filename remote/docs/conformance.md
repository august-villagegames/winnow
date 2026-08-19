# Winnow Remote MCP v1 conformance record

**Release-gate status: IN PROGRESS.** The remote implementation is deployed
and its automated checks pass, but no host is yet approved or advertised. A
configured connector or a successful one-round browser flow is not host
conformance.

Last evidence refresh: `2026-08-19`. This record intentionally contains no
public page URL, session identifier, seed, continuation, request/response
body, claim token, capability, publish fence, raw IP address, user agent, or
provider credential.

## Verified implementation baseline

| Check | Result |
| --- | --- |
| Portable Python suite | PASS — 56 tests |
| Browser-runtime Node suites | PASS — 37 tests |
| Remote unit/integration suite | PASS — 47 tests |
| Schema identity and diff checks | PASS |
| Real browser page after deployed remote create/update | Observed; public page reached a round-two completion screen after a browser-driven successor flow. |

The remote suite exercises the official Streamable HTTP service, strict
creation/wait/publish contracts, browser transitions, marker convergence,
ambiguous provider finalization, restart recovery, limits, quotas, CORS, and
capability isolation. It is necessary evidence, not a substitute for a live
host test.

## Host status

| Host | Status | Evidence and remaining release checks |
| --- | --- | --- |
| Claude Desktop | In progress — not approved | A deployed session published round one and then a successor that rendered in a normal browser. The connector contract and all actionable result handoffs were corrected after live testing. Still required: a second independent browser-driven cycle, proof that the same task resumes/re-enters wait with no chat input or per-round approval, cancellation, connection-loss, navigation, termination, deployed ingress provenance, quota-admission decision, byte-limit fit, and offering/prerequisite record. |
| Cowork | Not executed — not approved | Run the complete public-host harness. |
| Claude Code | Not executed — not approved | Run the complete public-host harness. |

No conclusion about a host may be inferred from a local probe, an SDK test, a
connector installation, or browser rendering alone.

## Required live evidence per host

Run [host-conformance-harness.md](host-conformance-harness.md) against the
deployed `/mcp` endpoint. Record only redacted outcome classes and timings:

1. Connector configuration and offering/prerequisite disclosure.
2. A public page/resource URL visible before the first wait.
3. Two independent browser-driven cycles in the same task: accepted request,
   resumed task without chat input, successor publication, and a following
   wait.
4. One-time approval, cancellation, connection loss, UI navigation, and
   process termination behavior.
5. Deployed ingress provenance category and the resulting quota-admission
   decision; never retain raw network or header values.
6. Actual request/response byte-limit fit.

Stop and omit the host from support claims if it requires an intervening chat
message, recurring approval, hidden URL, or unsafe shared-egress quota policy.

## Privacy and behavior statement

Winnow Remote adds no account, payment, advertising, sponsored alternative, or
server-side research. The public HereNow page contains comparison content and
committed verdict history, and expires on its original provider schedule.
Claim tokens, agent session handles, browser capabilities, and publish fences
are never placed in public URLs or page content. A public URL cannot recover
private agent state.

The historical Work Package 0 local browser/MCP probe and anonymous HereNow
observation remain useful transport/provider evidence, but they do not approve
any host. The current release gate is the live, per-host evidence above.
