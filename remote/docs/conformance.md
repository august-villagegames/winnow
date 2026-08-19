# Winnow Remote MCP v1 — Work Package 0 Conformance Record

**Result: BLOCKED.** This record establishes a clean repository baseline, a
local two-cycle MCP/browser proof, and a bounded live HereNow observation. It
does **not** establish the required same-live-agent flow in any advertised v1
host. No host is approved or advertised by this work package.

Last evidence refresh: `2026-08-18T15:29:25Z`. The final HereNow invocation
completed immediately before this timestamp; the final localhost probe started
at it.

This file intentionally contains no public page URL, slug, signed upload URL,
claim token, capability, request body, or response body.

## Scope and method

The disposable probes are deliberately not coordinator or production service
code:

- `remote/probes/mcp_browser_probe.py` is a stdlib-only localhost probe. It
  exposes a page URL, a JSON-RPC-shaped MCP endpoint, one bounded wait tool,
  and a browser release endpoint.
- `remote/probes/herenow_anonymous_probe.py` creates one generic,
  non-sensitive page, updates its same slug once, prints only redacted
  measurements, and leaves the disposable page to its documented anonymous
  expiry. Its per-request timeout and marker-polling window are both capped at
  30 seconds. Its test page is capped at 1 MiB.

The latest live HereNow run used a 10-second per-request cap, a 30-second
convergence window, a one-second poll interval, and a 1 MiB generic page. This
is one observation, not an asserted provider limit or throughput guarantee.

## Repository baseline

The applicable portable contract remains the existing immutable `4.0.0` seed
schema and legacy one-off publisher/runtime. The remote implementation has not
been started by this work package.

| Check | Observed result |
| --- | --- |
| `python3 -m unittest discover -s tests -v` | PASS — 53 tests |
| `node --test tests/runtime-core.test.mjs` | PASS — 29 tests |
| `python3 scripts/check_schema_identity.py` | PASS |
| `python3 -m py_compile remote/probes/mcp_browser_probe.py remote/probes/herenow_anonymous_probe.py` | PASS |

## Local MCP/browser probe

Command:

```sh
python3 remote/probes/mcp_browser_probe.py --self-test
```

Latest observed redacted result (at the evidence-refresh timestamp):

```json
{"result":"pass","cycles":2,"idleSeconds":0.15,"maxWaitSeconds":2,"browserPageVisibleBeforeWait":true}
```

Evidence established by this local-only test:

1. A browser-visible page request returned HTTP 200 before either wait began.
2. The endpoint answered `initialize` and `tools/list`, exposing only
   `wait_for_browser`.
3. In each of two cycles, an MCP `tools/call` waited with a two-second upper
   bound, then a browser `POST /browser/continue` released it.
4. Both waits returned `continue_requested`; the transient state recorded two
   resumptions.

Limit: this is an in-process localhost transport check. It does not prove
Streamable HTTP compatibility with an MCP SDK, public ingress behavior, a real
browser UI, task persistence, or any third-party host's unattended execution
policy.

## Live HereNow observation

Command:

```sh
python3 remote/probes/herenow_anonymous_probe.py \
  --request-timeout-seconds 10 \
  --convergence-window-seconds 30 \
  --poll-interval-seconds 1 \
  --target-bytes 1048576
```

| Subject | Observed result | Gate interpretation |
| --- | --- | --- |
| Invalid anonymous create (`files: []`) | HTTP 400; response key names were `code`, `details`, `docs_url`, `error`, and `message`. | Request validation exists; exact semantics require adapter tests later. |
| Anonymous create | HTTP 200. Response contained the expected anonymous/create/upload fields, including a claim token field; the token itself was never emitted. | PASS for this one create. |
| Claim-token shape | 64 characters; URL-safe-like character class. | Observed metadata only; treat as opaque, never as a stable format contract. |
| First upload/finalize | HTTP 200 / HTTP 200. | PASS for this one revision. |
| Same-slug update | HTTP 200, followed by upload/finalize HTTP 200 / HTTP 200. | PASS for this one update. |
| Live revision marker | First post-update fetch returned HTTP 200 with the second generic marker; convergence measured 371 ms. | PASS for this one cache/marker observation. |
| Cache headers | Post-update page returned `Cache-Control: no-cache` and an ETag. | Observed once; not a cache policy guarantee. |
| Tested payload | 1,048,576 bytes accepted for each generic `index.html` revision. Earlier WP0 evidence also accepted 131,072 bytes. | A supported 1 MiB sample, **not** a maximum payload boundary. The current docs list a 250 MB anonymous max-site-file limit; this probe deliberately remained far below it. |
| Initial expiry | Create response reported exactly 86,400 seconds remaining at creation. | PASS for an approximately 24-hour initial lifetime in this run. |
| Expiry preservation on update | The update response itself had no `expiresAt`, but the first finalize, update finalize, and idempotent finalize replay each exposed an expiry equal to the create expiry. | PASS for original-expiry preservation across this observed anonymous update. |
| Finalize retry | Repeating the same second `versionId` finalize returned HTTP 200 with `replayed: true`. | PASS for the documented idempotent finalize retry path. A true transport-timeout/ambiguous-finalize injection was not attempted. |
| Rate-limit signals | No rate-limit headers were observed on invalid create, create, update, upload, or three finalize responses; upload responses included ETags. | No limit was exercised. Current docs describe an anonymous-create error at 60 sites/hour; this probe used one anonymous create and did not stress that quota. |
| Provider documentation checked | Current docs state anonymous updates require the claim token and do not extend expiry; matching hashes are skipped on update; interrupted uploads can be re-presigned; finalize is idempotent by `versionId`. | Documentation supports the observed semantics, but it does not substitute for unexecuted failure cases. |

The probe is redacted by construction: its output contains only status codes,
field names, sizes, selected non-secret headers, and durations. The generic
anonymous page is intentionally left to expiry because no anonymous delete
endpoint was observed or assumed.

Current documentation was consulted on 2026-08-18 at
<https://here.now/docs>. It declares a 250 MB anonymous maximum site-file
size, describes a 60-anonymous-site/hour rate-limit error, and documents the
update/finalize semantics above. No provider rate limit was deliberately
triggered.

## Advertised host conformance

The required test is: connect the host to a deployed remote Streamable HTTP MCP
endpoint; display a returned page/resource URL; complete two browser-driven
wait/research/publish/wait cycles in the **same live task** with no intervening
chat input or approval; then exercise cancellation, connection loss, UI
navigation, and task termination.

No deployment, host connector configuration, or authenticated host session was
available in this work package. Therefore all entries are blocked rather than
failed: no negative product claim is made, but no positive conformance claim is
permitted. On 2026-08-18, the local Claude Code CLI was version `2.0.15`; it
listed only an unrelated connected `pencil` MCP server and reported `Invalid
API key · Please run /login`. Cowork was not installed.

| Host | Connector/authenticated state observed | Two-cycle same-live-agent evidence | Unattended approval / cancellation / navigation / termination evidence | Maximum reliable wait and server cap | Status |
| --- | --- | --- | --- | --- | --- |
| Claude | No public deployment endpoint, connector configuration, or authenticated Claude session was discoverable in this workspace. | Not run. | Not run. | Not measured; no value selected. | **BLOCKED** |
| Cowork | Cowork executable not installed; no connector configuration or authenticated session was discoverable. | Not run. | Not run. | Not measured; no value selected. | **BLOCKED** |
| Claude Code | CLI `2.0.15` available, but no Winnow endpoint was configured; only `pencil` was connected and authentication reported an invalid API key. | Not run. | Not run. | Not measured; no value selected. | **BLOCKED** |

Codex and ChatGPT are later-host packaging targets in the plan, not v1 hosts;
the local probe must not be interpreted as conformance evidence for either.

## Ingress, proxy, host offering, and shared-quota decisions

| Area | Current observed state | Decision / requirement carried forward |
| --- | --- | --- |
| Public ingress | No deployed service DNS name, TLS endpoint, or Streamable HTTP MCP ingress was exercised. | Do not select CORS or a public-origin configuration yet. |
| Reverse proxy | The local probe bound only `127.0.0.1`; no forwarding header was accepted or tested. | Deployment must use an explicit trusted-proxy allowlist and ignore client-supplied forwarding headers from other hops. |
| Host authentication | No host connector or MCP credential/approval configuration was observed. | Winnow itself remains accountless; each host may be offered only after its connector and one-time/unattended approval configuration pass the live test. |
| v1 host offering | No host meets the required same-task two-cycle gate. | Offer **no** Claude, Cowork, or Claude Code integration as supported v1 functionality. |
| End-user session quota | No ingress network identity can be observed without deployment. | Retain the planned 10 new sessions per normalized network/client bucket per UTC day as an unvalidated implementation requirement; do not persist raw IPs. |
| Shared HereNow quota | All anonymous publications appear to originate from the coordinator's egress network in the intended architecture. This one run supplied no rate-limit headers or threshold. | Require a global publish budget and circuit breaker. Do not select a numeric shared limit from this evidence; retain `normal`, `no_new_sessions`, `read_only_existing`, and `status_only` as required modes. |

## Final gate

**WP0 is BLOCKED; Work Package 1 must not begin.**

The local bounded-wait mechanism and two bounded anonymous HereNow
create/update samples are positive preliminary evidence. The second sample
verified original-expiry preservation through finalizer responses and exercised
the documented idempotent-finalize retry. However, the mandatory three-host
same-live-agent two-cycle conformance test has not been run. HereNow maximum
payload, rate-limit enforcement, and a network-induced ambiguous-finalize
reconciliation remain unverified. The next authorized action is to provision a
disposable deployed endpoint and run the recorded host matrix without changing
the product model or introducing managed research, Sampling, Tasks, or a
return-to-chat flow.
