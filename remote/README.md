# Winnow Remote service

`winnow-remote` is the separately deployable HTTPS boundary for rolling Winnow sessions. It exposes one Streamable HTTP MCP endpoint at `/mcp` and two browser endpoints under `/v1/session/`. It does not research, select options, or require a HereNow account/API key: it validates a supplied seed, coordinates browser choices, and creates or updates anonymous HereNow pages.

The service is not ready to advertise any MCP host. A public HTTPS deployment and the host-specific two-cycle gate in [docs/host-conformance-harness.md](docs/host-conformance-harness.md) remain required work.

## Local verification

The unit and integration suite uses a deterministic Redis-script evaluator and a fake HereNow service. It never sends a live request:

```sh
python3 -m pip install ./remote
PYTHONPATH=remote/src python3 -B -m unittest discover -s remote/tests -v
```

The `Dockerfile` is built from the repository root so it can include only the portable compiler assets the publisher needs:

```sh
docker build -f remote/Dockerfile -t winnow-remote:local .
```

Running a container requires the complete production configuration described in [docs/deployment.md](docs/deployment.md). There is intentionally no development fallback to in-memory state, plaintext Redis, generated keys, or an arbitrary public origin.

## Endpoints

| Endpoint | Purpose | Response privacy |
| --- | --- | --- |
| `POST /mcp` | Canonical Streamable HTTP MCP endpoint | MCP tool result only; no redirect to another MCP path |
| `GET /v1/session/status` | Browser state, with exact allowed HereNow origin and bearer header | Session-scoped, `no-store` |
| `POST /v1/session/next-round` | Browser verdict submission, with exact allowed HereNow origin and bearer header | Session-scoped, `no-store` |
| `GET /healthz` | Process liveness | Constant `{"status":"ok"}`; no session/store data |
| `GET /readyz` | Redis reachability | Constant `{"status":"ready"}` or `{"status":"unavailable"}`; no session/store data |

`/readyz` is for the platform load balancer; it performs only Redis `PING` and does not list, count, or inspect sessions. The image runs as UID/GID `10001`, disables Uvicorn access logs, and starts Uvicorn with proxy-header processing disabled so the application—not the server—owns its explicit trusted-proxy policy.

