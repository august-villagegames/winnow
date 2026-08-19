"""Transport policy and narrow operational adapters for Winnow Remote.

Settings deliberately contain transport policy only.  They never retain page
content, capabilities, claim tokens, or raw client IP addresses.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import hmac
import ipaddress
import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

from .coordinator import CoordinatorError, normalize_network_prefix
from .repository import TransactionalRepository


class TransportPolicyError(ValueError):
    """A generic transport-policy error whose text contains no request input."""


@dataclass(frozen=True)
class RequestProvenance:
    """Coarse source data approved by the ingress policy, never durable raw IP."""

    network_prefix: str
    client_family: str


_CURRENT_MCP_PROVENANCE: contextvars.ContextVar[RequestProvenance | None] = contextvars.ContextVar(
    "winnow_remote_mcp_provenance", default=None
)


def current_mcp_provenance() -> RequestProvenance:
    value = _CURRENT_MCP_PROVENANCE.get()
    if value is None:
        raise TransportPolicyError("trusted transport provenance is unavailable")
    return value


@dataclass(frozen=True)
class TrustedProxyPolicy:
    """Only accept a forwarded client address from a declared direct proxy.

    Deployment config must list the exact proxy networks that overwrite
    ``X-Forwarded-For`` with one IP literal.  Requests from any other peer use
    the ASGI peer address and all forwarding headers are ignored.
    """

    trusted_proxy_networks: tuple[str, ...] = ()
    _trusted_networks: tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        parsed = []
        for value in self.trusted_proxy_networks:
            try:
                parsed.append(ipaddress.ip_network(value, strict=True))
            except ValueError as exc:
                raise ValueError("trusted proxy network is invalid") from exc
        object.__setattr__(self, "_trusted_networks", tuple(parsed))

    def provenance(self, scope: Mapping[str, Any]) -> RequestProvenance:
        peer = self._peer_address(scope)
        address = self._forwarded_address(scope) if self._is_trusted_proxy(peer) else peer
        try:
            prefix = normalize_network_prefix(address)
        except CoordinatorError as exc:
            raise TransportPolicyError("request source is unavailable") from exc
        return RequestProvenance(network_prefix=prefix, client_family=self._client_family(scope))

    @staticmethod
    def _peer_address(scope: Mapping[str, Any]) -> str:
        client = scope.get("client")
        if not isinstance(client, (tuple, list)) or not client or not isinstance(client[0], str):
            raise TransportPolicyError("request source is unavailable")
        try:
            return str(ipaddress.ip_address(client[0]))
        except ValueError as exc:
            raise TransportPolicyError("request source is unavailable") from exc

    def _is_trusted_proxy(self, address: str) -> bool:
        parsed = ipaddress.ip_address(address)
        return any(parsed in network for network in self._trusted_networks)

    @staticmethod
    def _header_values(scope: Mapping[str, Any], name: bytes) -> list[str]:
        headers = scope.get("headers", [])
        values = []
        if not isinstance(headers, list):
            return values
        for pair in headers:
            if not isinstance(pair, (tuple, list)) or len(pair) != 2 or pair[0].lower() != name:
                continue
            try:
                values.append(pair[1].decode("latin-1"))
            except UnicodeDecodeError:
                continue
        return values

    def _forwarded_address(self, scope: Mapping[str, Any]) -> str:
        values = self._header_values(scope, b"x-forwarded-for")
        # More than one header or a chain means the ingress invariant was not
        # met.  Fall back to the trusted proxy address instead of guessing.
        if len(values) != 1 or "," in values[0]:
            return self._peer_address(scope)
        candidate = values[0].strip()
        try:
            return str(ipaddress.ip_address(candidate))
        except ValueError:
            return self._peer_address(scope)

    def _client_family(self, scope: Mapping[str, Any]) -> str:
        values = self._header_values(scope, b"user-agent")
        user_agent = values[0].lower() if len(values) == 1 else ""
        if "anthropic" in user_agent or "claude" in user_agent:
            return "anthropic"
        if "openai" in user_agent or "chatgpt" in user_agent or "codex" in user_agent:
            return "openai"
        return "other"


class RateLimitError(RuntimeError):
    """The request exceeded a bounded transport limit."""


@dataclass(frozen=True)
class RateLimit:
    limit: int
    window_seconds: int

    def __post_init__(self) -> None:
        if self.limit < 1 or self.window_seconds < 1:
            raise ValueError("rate limit is invalid")


@dataclass(frozen=True)
class RateLimitPolicy:
    status: RateLimit = RateLimit(limit=60, window_seconds=60)
    next_round: RateLimit = RateLimit(limit=6, window_seconds=60)
    mcp_create: RateLimit = RateLimit(limit=10, window_seconds=60)
    mcp_publish: RateLimit = RateLimit(limit=6, window_seconds=60)


class RateLimiter:
    """Rate-limit opaque network/capability subjects through the durable store."""

    def __init__(self, repository: TransactionalRepository, *, hmac_key: bytes, policy: RateLimitPolicy = RateLimitPolicy(), now: Callable[[], float] = time.time) -> None:
        if not isinstance(hmac_key, bytes) or len(hmac_key) < 32:
            raise ValueError("rate-limit HMAC key must contain at least 256 bits")
        self._repository = repository
        self._hmac_key = hmac_key
        self._policy = policy
        self._now = now

    def check(self, category: str, subject: str, limit: RateLimit) -> None:
        if not isinstance(subject, str) or not subject or len(subject) > 512:
            raise TransportPolicyError("rate subject is unavailable")
        now = self._now()
        window_end = (int(now) // limit.window_seconds + 1) * limit.window_seconds
        digest = hmac.new(
            self._hmac_key,
            f"{category}\\0{subject}\\0{window_end}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        if not self._repository.increment_rate_limit(digest, expires_at=float(window_end), limit=limit.limit):
            raise RateLimitError("rate limit exceeded")

    def status(self, browser_capability: str) -> None:
        self.check("browser-status", browser_capability, self._policy.status)

    def next_round(self, browser_capability: str) -> None:
        self.check("browser-next-round", browser_capability, self._policy.next_round)

    def mcp_create(self, provenance: RequestProvenance) -> None:
        self.check("mcp-create", f"{provenance.network_prefix}\\0{provenance.client_family}", self._policy.mcp_create)

    def mcp_publish(self, session_handle: str) -> None:
        self.check("mcp-publish", session_handle, self._policy.mcp_publish)


class WaitNotifier(Protocol):
    """Best-effort Redis wakeup with polling as the correctness backstop."""

    def notify(self, session_id: str) -> None: ...

    async def wait(self, session_id: str, timeout_seconds: float) -> None: ...


class PollingWaitNotifier:
    """Safe fallback: no signal is required because the waiter always polls."""

    def notify(self, session_id: str) -> None:
        return None

    async def wait(self, session_id: str, timeout_seconds: float) -> None:
        await asyncio.sleep(max(0.0, timeout_seconds))


class InProcessWaitNotifier:
    """Test/local notifier with the same lossy-notification semantics as Redis."""

    def __init__(self) -> None:
        self._events: dict[str, asyncio.Event] = {}

    def notify(self, session_id: str) -> None:
        self._events.setdefault(session_id, asyncio.Event()).set()

    async def wait(self, session_id: str, timeout_seconds: float) -> None:
        event = self._events.setdefault(session_id, asyncio.Event())
        try:
            await asyncio.wait_for(event.wait(), timeout=max(0.0, timeout_seconds))
        except TimeoutError:
            return
        finally:
            event.clear()


class RedisNotificationClient(Protocol):
    def publish(self, channel: str, message: str) -> Any: ...

    def pubsub(self, **kwargs: Any) -> Any: ...


class RedisWaitNotifier:
    """Redis Pub/Sub wakeups; service polling makes missed messages harmless."""

    def __init__(self, client: RedisNotificationClient, *, prefix: str = "winnow:remote:v1:wait") -> None:
        self._client = client
        self._prefix = prefix.rstrip(":")

    def notify(self, session_id: str) -> None:
        try:
            self._client.publish(self._channel(session_id), "1")
        except Exception:
            # Pub/Sub is latency only, never event durability.  The next poll
            # reads the coordinator's CAS-persisted event.
            return

    async def wait(self, session_id: str, timeout_seconds: float) -> None:
        await asyncio.to_thread(self._wait_sync, session_id, max(0.0, timeout_seconds))

    def _wait_sync(self, session_id: str, timeout_seconds: float) -> None:
        try:
            pubsub = self._client.pubsub(ignore_subscribe_messages=True)
            try:
                pubsub.subscribe(self._channel(session_id))
                pubsub.get_message(timeout=timeout_seconds)
            finally:
                close = getattr(pubsub, "close", None)
                if callable(close):
                    close()
        except Exception:
            # Preserve the bounded long-poll even if Redis temporarily cannot
            # publish/subscribe.  The caller immediately reads state again.
            time.sleep(min(timeout_seconds, 0.05))

    def _channel(self, session_id: str) -> str:
        # Session IDs are server generated, but hash anyway so an operational
        # Redis monitor cannot correlate a channel with page metadata.
        digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        return f"{self._prefix}:{digest}"
