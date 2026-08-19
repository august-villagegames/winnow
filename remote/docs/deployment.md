# Provider-neutral deployment and operations

This runbook is for the Winnow Remote service only. It does not choose a cloud provider, create infrastructure, deploy an endpoint, or establish host conformance. Treat every example value as a placeholder, never as a secret.

## Preconditions

Before exposing the service, provide all of the following:

1. One managed Redis-compatible service with TLS enforced, authentication, private network reachability, and no public unauthenticated listener.
2. A public DNS name and TLS-terminating ingress. The endpoint must be exactly `https://<public-name>/mcp`; it must not redirect, rewrite, or buffer the Streamable HTTP request/response path.
3. An ingress that passes the original `Host`, preserves long requests, and uses one known proxy address to connect to the container. Configure that proxy to replace—not append—`X-Forwarded-For` with one IP literal. Do not trust `X-Forwarded-For` from any other direct peer.
4. A secrets manager capable of injecting values as environment variables without printing them in deployment logs, shell history, crash reports, or support tickets.
5. Egress control that permits DNS, the managed Redis endpoint, and HTTPS only to approved public destinations. HereNow, signed-upload hosts, public page verification, and user-supplied hosted-image URLs need outbound HTTPS; block loopback, link-local, RFC1918, metadata, and every non-HTTPS port at the network layer as well as relying on the in-process SSRF checks.

The service needs no HereNow account, API key, payment, or user identity. The anonymous create/update calls use only the one-time server-side claim token returned by HereNow; it is AEAD-encrypted in Redis and never enters an MCP result, browser URL, metric, log, trace, or manual-test output.

## Required environment

All secret values use **unpadded base64url**. Provision random, independent 32-byte HMAC keys. `WINNOW_REMOTE_AEAD_KEYS_JSON` maps an operator-owned key identifier to a 16-, 24-, or 32-byte AES key encoded the same way; use 32 bytes for new keys. Never use the same material for two variables.

| Variable | Secret | Meaning |
| --- | --- | --- |
| `WINNOW_REMOTE_REDIS_URL` | Yes | `rediss://` managed Redis URL. Plain `redis://`, URL query parameters that could weaken TLS, and fragments are rejected at startup. A password and ACL username are allowed only inside this injected value. |
| `WINNOW_REMOTE_REDIS_PREFIX` | No | Redis namespace; default `winnow:remote:v1`. Use a unique namespace per environment. |
| `WINNOW_REMOTE_COORDINATOR_ORIGIN` | No | Exact public HTTPS origin, such as `https://mcp.example.org`; no path, query, fragment, or non-443 port. |
| `WINNOW_REMOTE_MCP_ALLOWED_HOSTS` | No | Comma-separated exact `Host` values accepted by the MCP transport, such as `mcp.example.org`. |
| `WINNOW_REMOTE_TRUSTED_PROXY_CIDRS` | No | Comma-separated ingress proxy CIDRs. Empty means forwarding headers are ignored. |
| `WINNOW_REMOTE_HERENOW_HOST_SUFFIXES` | No | Comma-separated anonymous-page host suffixes; default `.here.now`. Change only after validating exact HereNow public origins. |
| `WINNOW_REMOTE_CAPABILITY_HMAC_KEY_B64` | Yes | Separate 32-byte capability-derivation/hash key. |
| `WINNOW_REMOTE_RATE_LIMIT_HMAC_KEY_B64` | Yes | Separate 32-byte key for rate-limit bucket digests. |
| `WINNOW_REMOTE_QUOTA_HMAC_KEY_B64` | Yes | Separate 32-byte key for daily creation-quota bucket digests. |
| `WINNOW_REMOTE_ACTIVE_AEAD_KEY_ID` | No | The key identifier used for newly persisted HereNow claims. |
| `WINNOW_REMOTE_AEAD_KEYS_JSON` | Yes | JSON object containing the active key and every old key still required to decrypt live records. |
| `WINNOW_REMOTE_MAX_WAIT_SECONDS` | No | Bounded MCP wait, default `300`, allowed `1..900`. |
| `WINNOW_REMOTE_WAIT_RENEWAL_GRACE_SECONDS` | No | Renewed-wait grace, default `15`, allowed `0..300`. |
| `WINNOW_REMOTE_CREATION_HANDOFF_SECONDS` | No | Initial agent handoff deadline, default `300`, allowed `1..3600`. |
| `WINNOW_REMOTE_RESEARCH_DEADLINE_SECONDS` | No | Time allowed to return a successor, default `1800`, allowed `1..86400`. |
| `WINNOW_REMOTE_CREATING_TTL_SECONDS` | No | TTL for a partially-created record, default `900`, allowed `1..3600`. |
| `WINNOW_REMOTE_DAILY_QUOTA_LIMIT` | No | Per network/client-family anonymous creation budget, default `10`, allowed `1..100`. Subject to WP7 ingress evidence. |
| `WINNOW_REMOTE_RATE_STATUS_PER_MINUTE` | No | Browser status limit, default `60`. |
| `WINNOW_REMOTE_RATE_NEXT_ROUND_PER_MINUTE` | No | Browser verdict limit, default `6`. |
| `WINNOW_REMOTE_RATE_MCP_CREATE_PER_MINUTE` | No | MCP create limit, default `10`. |
| `WINNOW_REMOTE_RATE_MCP_PUBLISH_PER_MINUTE` | No | MCP publish limit, default `6`. |
| `WINNOW_REMOTE_CIRCUIT_MODE` | No | One of `normal`, `no_new_sessions`, `read_only_existing`, or `status_only`; default `normal`. |
| `WINNOW_REMOTE_REDIS_TIMEOUT_SECONDS` | No | Redis connect/read timeout, default `5`, allowed `1..30`. |

An environment with a missing, malformed, undersized, or inconsistent key fails closed at process startup. Keep `repr`/diagnostics of the runtime settings redacted; never add a configuration dump endpoint.

### Key rotation

First add a new AEAD key to `WINNOW_REMOTE_AEAD_KEYS_JSON`, deploy it everywhere while keeping the previous active key, then change `WINNOW_REMOTE_ACTIVE_AEAD_KEY_ID`. Retain old decryption keys for the maximum live-session/backup retention window plus the approved restoration margin. Do not remove an old key merely because a rollout finished. HMAC keys are not transparently rotatable because their derived bearer, index, quota, and rate bucket values depend on them; rotate them only through an explicit session-expiry maintenance plan.

## Ingress, TLS, and scaling

Terminate TLS at the selected edge with TLS 1.2+ and redirect HTTP to HTTPS before the service. The pod/service itself should remain private. Send only the public DNS hostname in `Host`; the app's MCP allowed-host list is not a wildcard. Disable proxy buffering and set its upstream idle/read timeout above the maximum wait plus a margin (at least 930 seconds for the default max).

The app must receive the actual immediate proxy address as its ASGI peer. List only that proxy CIDR in `WINNOW_REMOTE_TRUSTED_PROXY_CIDRS`; a direct request or unrecognized proxy ignores forwarding headers. If an ingress cannot guarantee a single replacement `X-Forwarded-For` value, leave its CIDR out and treat the direct proxy network as the quota source. Work Package 7 must measure each MCP host's actual provenance before enabling anonymous creation for it.

Run one Uvicorn process per container and scale replicas against the same Redis store. Pub/Sub is only a wakeup optimization: durable state and polling make a worker restart or cross-replica delivery safe. `/healthz` is liveness only; route traffic only to replicas returning HTTP 200 from `/readyz`.

Circuit mode is startup configuration. To change it safely across a scaled deployment, first drain/remove every old replica from ingress, then start only the new mode and confirm readiness before restoring traffic. Do not rely on a mixed rolling update to enforce a global emergency mode. Drill each mode with a disposable session before a release and record only status counts/timestamps.

## Privacy-safe operations

The container already starts Uvicorn with `--no-access-log`. Configure the ingress, APM agent, error reporter, and platform logs to omit request bodies, request/response headers, query strings, URLs after the origin, exception arguments, Redis commands/values, and environment variables. Do not enable packet capture, debug HTTP logging, Redis MONITOR, command tracing, heap dumps, or automatic request recording in this workload.

Allowed application telemetry is content-free and low-cardinality: endpoint class, HTTP status class, tool name, outcome class, duration bucket, Redis reachability, circuit mode, queue/wait duration bucket, and process/restart count. Do not attach session identifiers, seeds, seed hashes, browser or agent capabilities, claims, slugs, HereNow URLs, source URLs, IPs, user agents, or free-form errors as labels, attributes, exemplars, or breadcrumbs. Retain content-free operational logs/traces no longer than 7 days unless a stricter policy applies; do not sample error payloads.

Alert on sustained `/readyz` failures, Redis connection errors, 5xx rate, HereNow rejection/timeout rate (count only), abnormal anonymous-create volume, rate/quota denial spikes, browser-to-wait latency SLO breaches, and unexpected circuit mode. Alert records must link to the runbook rather than embedding a request sample.

## Redis retention, backup, and egress posture

Redis is a 24-hour-ephemeral data store. Enforce its own TLS, authentication, private endpoint, encryption at rest, and automated deletion/expiry. The repository sets TTLs on session indexes and counters; verify the managed service's eviction policy does not evict active keys before their TTL.

Disable backups containing this namespace where the platform permits it. If a backup is mandatory, encrypt it, scope it to this isolated namespace, retain it for no more than 24 hours, and test automatic expiry/deletion. Backups, replicas, logs, cache exports, and point-in-time recovery must not silently extend the claim-token/session retention period. Grant operators least privilege and never use `MONITOR` in normal operation.

At the network layer, allow only:

- DNS to the approved resolver;
- TLS Redis to the configured managed endpoint; and
- outbound TCP 443 through an egress control that rejects private, link-local, metadata, and non-HTTPS destinations.

The image verifier additionally pins and validates public destinations, but it is not a substitute for a default-deny egress policy. Configure provider- and environment-specific egress controls before exposure.
