"""Composable ASGI application for the remote Streamable HTTP MCP service."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from .browser_api import BrowserApi, BrowserApiConfig
from .coordinator import Coordinator
from .herenow import HereNowPublisher, MAX_REMOTE_MCP_REQUEST_BYTES
from .mcp_tools import McpToolConfig, McpToolService, register_mcp_tools
from .settings import _CURRENT_MCP_PROVENANCE, RateLimiter, TrustedProxyPolicy, WaitNotifier


ASGIApp = Callable[[Mapping[str, Any], Callable[[], Awaitable[Mapping[str, Any]]], Callable[[Mapping[str, Any]], Awaitable[None]]], Awaitable[None]]


def _always_ready() -> bool:
    """Default for unit-only composition roots without a durable dependency."""

    return True


@dataclass(frozen=True)
class AppConfig:
    """Non-secret endpoint policy; deployment injects secrets and clients separately."""

    coordinator_origin: str
    mcp_allowed_hosts: tuple[str, ...]
    trusted_proxy_policy: TrustedProxyPolicy = TrustedProxyPolicy()
    browser: BrowserApiConfig = BrowserApiConfig()
    mcp_max_wait_seconds: int = 300
    mcp_wait_poll_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not isinstance(self.coordinator_origin, str) or not self.coordinator_origin.startswith("https://"):
            raise ValueError("coordinator origin must be HTTPS")
        if not self.mcp_allowed_hosts or any(not isinstance(item, str) or not item for item in self.mcp_allowed_hosts):
            raise ValueError("MCP allowed hosts must be explicit")


@dataclass(frozen=True)
class AppDependencies:
    """Explicit composition root; no implicit Redis, keys, or provider clients."""

    coordinator: Coordinator
    publisher_factory: Callable[[Callable[[Mapping[str, Any], str | None], bytes]], HereNowPublisher]
    rate_limiter: RateLimiter
    notifier: WaitNotifier
    readiness_probe: Callable[[], bool] = _always_ready


class McpIngressGuard:
    """Apply input limits and trusted source binding before SDK JSON parsing."""

    def __init__(self, app: ASGIApp, *, trusted_proxy_policy: TrustedProxyPolicy, max_request_bytes: int = MAX_REMOTE_MCP_REQUEST_BYTES) -> None:
        self._app = app
        self._trusted_proxy_policy = trusted_proxy_policy
        self._max_request_bytes = max_request_bytes

    async def __call__(self, scope: Mapping[str, Any], receive: Callable[[], Awaitable[Mapping[str, Any]]], send: Callable[[Mapping[str, Any]], Awaitable[None]]) -> None:
        if scope.get("type") != "http":
            await self._app(scope, receive, send)
            return
        try:
            provenance = self._trusted_proxy_policy.provenance(scope)
        except Exception:
            await self._reject(send, 400)
            return
        if scope.get("method") == "POST":
            headers = self._headers(scope)
            content_type = headers.get(b"content-type", b"").decode("latin-1", "ignore").split(";", 1)[0].strip().lower()
            declared = headers.get(b"content-length")
            if content_type != "application/json" or (declared is not None and (not declared.isdecimal() or int(declared) > self._max_request_bytes)):
                await self._reject(send, 415 if content_type != "application/json" else 413)
                return
            try:
                body = await self._read_bounded(receive)
                self._validate_tool_shape(body)
            except _RequestRejected as exc:
                await self._reject(send, exc.status)
                return
            receive = self._replay_body(body)
        token = _CURRENT_MCP_PROVENANCE.set(provenance)
        try:
            await self._app(scope, receive, send)
        finally:
            _CURRENT_MCP_PROVENANCE.reset(token)

    async def _read_bounded(self, receive: Callable[[], Awaitable[Mapping[str, Any]]]) -> bytes:
        chunks = bytearray()
        while True:
            message = await receive()
            if message.get("type") == "http.disconnect":
                raise _RequestRejected(400)
            if message.get("type") != "http.request":
                raise _RequestRejected(400)
            body = message.get("body", b"")
            if not isinstance(body, bytes):
                raise _RequestRejected(400)
            chunks.extend(body)
            if len(chunks) > self._max_request_bytes:
                raise _RequestRejected(413)
            if not message.get("more_body", False):
                return bytes(chunks)

    @staticmethod
    def _replay_body(body: bytes) -> Callable[[], Awaitable[Mapping[str, Any]]]:
        sent = False

        async def replay() -> Mapping[str, Any]:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": body, "more_body": False}

        return replay

    @staticmethod
    def _validate_tool_shape(body: bytes) -> None:
        def no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            value: dict[str, Any] = {}
            for key, item in pairs:
                if key in value:
                    raise _RequestRejected(400)
                value[key] = item
            return value

        try:
            request = json.loads(body.decode("utf-8"), object_pairs_hook=no_duplicate_object)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
            raise _RequestRejected(400) from exc
        if not isinstance(request, dict):
            raise _RequestRejected(400)
        if request.get("method") != "tools/call":
            return
        params = request.get("params")
        if not isinstance(params, dict):
            raise _RequestRejected(400)
        name = params.get("name")
        arguments = params.get("arguments")
        expected = {
            "create_winnow_session": {"seed", "mode"},
            "wait_for_continue": {"sessionHandle", "expectedRoundNumber", "expectedSeedHash", "maxWaitSeconds"},
            "publish_next_round": {"sessionHandle", "eventId", "publishFence", "parentSeedHash", "nextSeed"},
        }.get(name)
        if expected is not None and (not isinstance(arguments, dict) or set(arguments) != expected):
            raise _RequestRejected(400)

    @staticmethod
    def _headers(scope: Mapping[str, Any]) -> dict[bytes, bytes]:
        pairs = scope.get("headers", [])
        result: dict[bytes, bytes] = {}
        if isinstance(pairs, list):
            for pair in pairs:
                if isinstance(pair, (tuple, list)) and len(pair) == 2 and isinstance(pair[0], bytes) and isinstance(pair[1], bytes):
                    result[pair[0].lower()] = pair[1]
        return result

    @staticmethod
    async def _reject(send: Callable[[Mapping[str, Any]], Awaitable[None]], status: int) -> None:
        response = JSONResponse({"error": "request_rejected"}, status_code=status, headers={"Cache-Control": "no-store"})
        async def empty_receive() -> Mapping[str, Any]:
            return {"type": "http.disconnect"}

        await response({"type": "http"}, empty_receive, send)


class _RequestRejected(Exception):
    def __init__(self, status: int) -> None:
        self.status = status


def create_app(config: AppConfig, dependencies: AppDependencies) -> Starlette:
    """Build one official-SDK MCP surface plus browser routes in one ASGI app."""

    service = McpToolService(
        dependencies.coordinator,
        publisher_factory=dependencies.publisher_factory,
        rate_limiter=dependencies.rate_limiter,
        notifier=dependencies.notifier,
        config=McpToolConfig(
            coordinator_origin=config.coordinator_origin,
            max_wait_seconds=config.mcp_max_wait_seconds,
            wait_poll_seconds=config.mcp_wait_poll_seconds,
        ),
    )
    mcp = MCPServer(
        name="Winnow Remote",
        description="Model-free anonymous Winnow rolling-session coordinator.",
        instructions="Winnow never researches or invokes a model. Keep the originating task alive through each wait/research/publish cycle.",
        version="1",
    )
    register_mcp_tools(mcp, service)
    # The SDK owns the Streamable HTTP protocol.  Its hostname allowlist is
    # explicit because the deployment endpoint is configured outside source.
    mcp_app = mcp.streamable_http_app(
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
        max_request_body_size=MAX_REMOTE_MCP_REQUEST_BYTES,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(config.mcp_allowed_hosts),
            # Remote MCP clients generally send no Origin. Browser traffic is
            # separate and has exact per-session CORS below.
            allowed_origins=[],
        ),
        host="0.0.0.0",
    )
    browser = BrowserApi(
        dependencies.coordinator,
        rate_limiter=dependencies.rate_limiter,
        notifier=dependencies.notifier,
        config=config.browser,
    )

    async def health(_request: Any) -> JSONResponse:
        # This endpoint is intentionally constant.  It is suitable for a
        # process liveness check and must never expose store/session state.
        return JSONResponse({"status": "ok"}, headers={"Cache-Control": "no-store"})

    async def readiness(_request: Any) -> JSONResponse:
        # Redis is the only durable dependency.  Deliberately collapse every
        # failure to one opaque status so health checks cannot become a store
        # inspection endpoint.
        try:
            ready = dependencies.readiness_probe()
        except Exception:
            ready = False
        return JSONResponse(
            {"status": "ready" if ready else "unavailable"},
            status_code=200 if ready else 503,
            headers={"Cache-Control": "no-store"},
        )

    @contextlib.asynccontextmanager
    async def lifespan(_app: Starlette):
        # Mounting an SDK app does not run its nested lifespan. The official
        # session manager owns request cancellation and Streamable HTTP state.
        async with mcp.session_manager.run():
            yield

    return Starlette(
        # Mount the SDK app at the root so its native `/mcp` route remains the
        # canonical no-redirect endpoint. Browser routes are matched first.
        routes=[
            Route("/healthz", health, methods=["GET"]),
            Route("/readyz", readiness, methods=["GET"]),
            *browser.routes(),
            Mount("", app=McpIngressGuard(mcp_app, trusted_proxy_policy=config.trusted_proxy_policy)),
        ],
        lifespan=lifespan,
    )
