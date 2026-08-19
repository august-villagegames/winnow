# Work Package 7 public-host conformance harness requirements

This document prepares the deployment handoff. It deliberately does not run a host test, select a cloud provider, or claim any host is supported.

## Required deployment handoff

The operator supplies a disposable, public HTTPS origin after deployment:

```text
https://<public-name>/mcp
```

The discovered `create_winnow_session` tool accepts exactly two arguments:
`seed`, containing a valid Winnow v4 round-one seed, and `mode`, whose only
valid value is the literal string `"rolling"`. There is no `"publish"` or
`"live"` mode. This constraint is advertised in the MCP tool schema and
description so a host does not need an out-of-band prompt to discover it.

Rejected tool calls may provide only fixed, safe contract guidance:
`invalid_mode`, `invalid_request`, or `invalid_seed`. They never include seed
content, provider failures, capabilities, session handles, or internal state.

On a successful create or successor publish, the result contains both a public
resource link and a standard text content block with the exact private
`wait_for_continue` argument object. That success-only block is annotated for
the assistant audience; host conformance must verify that it reaches the model
without becoming ordinary assistant prose. The public URL cannot be used to
recover this state.

Before handing it to a host tester, verify the following without user content:

- `POST /mcp` reaches the official Streamable HTTP application directly (no redirect, auth wall, HTML interstitial, proxy buffering, or path rewrite);
- the public certificate and the configured MCP allowed-host value match;
- `GET /healthz` is 200 and `GET /readyz` is 200 after managed Redis is connected;
- one disposable anonymous HereNow smoke has passed using [herenow-smoke.md](herenow-smoke.md); and
- logs are redacted as required by [deployment.md](deployment.md).

Do not use `remote/probes/mcp_browser_probe.py` as the deployed endpoint. It is a local Work Package 0 transport experiment, not the real Streamable HTTP service. Work Package 7 connects each host to the actual `/mcp` endpoint.

## Per-host execution record

For **each** of Claude, Cowork, and Claude Code, start a fresh task and record only the following redacted facts:

1. Host product/version/configuration, whether ordinary host access is free, paid/pass-based, or ad-supported, and the exact user-facing prerequisite disclosure. Winnow itself adds no payment, advertising, or sponsored path.
2. Endpoint configuration success and a non-sensitive page/resource URL shown in the task before its first wait.
3. Two independent browser-triggered cycles: representative idle interval; browser request accepted; same task resumes without chat input; second MCP tool call occurs; task re-enters a second wait. Record timings and outcome classes, not payloads or IDs.
4. One-time/unattended approval behavior; cancellation; connection loss; UI navigation; and process termination behavior.
5. Effective ingress provenance category observed by the service: direct transport source, recognized trusted-proxy source, or shared host egress. Record header names/trust-chain category only—never raw IPs, header values, user agents, or request bodies.
6. Whether transport byte limits fit the actual deployed request/response budgets, and the resulting quota-admission decision for any shared egress.

The test must be stopped if a host requires return-to-chat input for the second cycle, per-round approval, hidden URL behavior, or an unsafe provenance/quota decision. A passing local unit test, local probe, or successful connector setup is not host conformance. Work Package 7 owns the final evidence record and all supported-host/documentation claims.
