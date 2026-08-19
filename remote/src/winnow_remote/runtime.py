"""Production composition and fail-closed environment configuration.

This module is the only supported runtime entry point.  Domain modules remain
free of environment reads so local integration tests can use deterministic
fakes, while a deployed process receives one managed TLS Redis client and all
security policy explicitly at startup.
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import time
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from starlette.applications import Starlette

from .app import AppConfig, AppDependencies, create_app
from .browser_api import BrowserApiConfig
from .coordinator import Coordinator, CoordinatorConfig
from .herenow import HereNowPublisher
from .repository import RedisRepository
from .security import CapabilitySecurity
from .settings import (
    RateLimit,
    RateLimiter,
    RateLimitPolicy,
    RedisWaitNotifier,
    TrustedProxyPolicy,
)


class RuntimeConfigurationError(ValueError):
    """An opaque configuration error that never contains an environment value."""


def _required(environ: Mapping[str, str], name: str) -> str:
    value = environ.get(name)
    if not isinstance(value, str) or not value:
        raise RuntimeConfigurationError(f"{name} must be set")
    return value


def _integer(environ: Mapping[str, str], name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = environ.get(name, str(default))
    if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
        raise RuntimeConfigurationError(f"{name} must be an integer")
    value = int(raw)
    if value < minimum or value > maximum:
        raise RuntimeConfigurationError(f"{name} is outside its allowed range")
    return value


def _csv(environ: Mapping[str, str], name: str, *, required: bool = False) -> tuple[str, ...]:
    raw = environ.get(name, "")
    if not isinstance(raw, str):
        raise RuntimeConfigurationError(f"{name} is invalid")
    values = tuple(item.strip() for item in raw.split(",") if item.strip())
    if required and not values:
        raise RuntimeConfigurationError(f"{name} must list at least one value")
    if len(values) != len(set(values)):
        raise RuntimeConfigurationError(f"{name} must not contain duplicates")
    return values


def _base64url_secret(environ: Mapping[str, str], name: str, *, lengths: frozenset[int]) -> bytes:
    raw = _required(environ, name)
    if len(raw) > 8192 or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in raw):
        raise RuntimeConfigurationError(f"{name} is not base64url")
    try:
        value = base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, binascii.Error) as exc:
        raise RuntimeConfigurationError(f"{name} is not base64url") from exc
    if len(value) not in lengths:
        raise RuntimeConfigurationError(f"{name} has an invalid length")
    return value


def _https_origin(value: str, *, name: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise RuntimeConfigurationError(f"{name} must be an HTTPS origin") from exc
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise RuntimeConfigurationError(f"{name} must be an HTTPS origin")
    try:
        port = parsed.port
    except ValueError as exc:
        raise RuntimeConfigurationError(f"{name} must be an HTTPS origin") from exc
    if port not in {None, 443}:
        raise RuntimeConfigurationError(f"{name} must be an HTTPS origin")
    return f"https://{parsed.hostname.lower()}"


def _redis_url(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
    except ValueError as exc:
        raise RuntimeConfigurationError("WINNOW_REMOTE_REDIS_URL must be a TLS Redis URL") from exc
    if parsed.scheme != "rediss" or not parsed.hostname or parsed.query or parsed.fragment:
        raise RuntimeConfigurationError("WINNOW_REMOTE_REDIS_URL must be a TLS Redis URL")
    # Passwords belong in the URL only as an injected secret.  Never parse or
    # reproduce it in errors/logs; redis-py receives the original string.
    return value


def _aead_keys(environ: Mapping[str, str]) -> dict[str, bytes]:
    raw = _required(environ, "WINNOW_REMOTE_AEAD_KEYS_JSON")
    try:
        decoded = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeConfigurationError("WINNOW_REMOTE_AEAD_KEYS_JSON must be an object") from exc
    if not isinstance(decoded, dict) or not decoded:
        raise RuntimeConfigurationError("WINNOW_REMOTE_AEAD_KEYS_JSON must be a non-empty object")
    keys: dict[str, bytes] = {}
    for key_id, encoded in decoded.items():
        if not isinstance(key_id, str) or not key_id or len(key_id) > 128 or not isinstance(encoded, str):
            raise RuntimeConfigurationError("WINNOW_REMOTE_AEAD_KEYS_JSON is invalid")
        # Reuse the secret parser without putting each input into process-wide
        # state or error text.
        keys[key_id] = _base64url_secret({"key": encoded}, "key", lengths=frozenset({16, 24, 32}))
    return keys


@dataclass(frozen=True)
class RuntimeSettings:
    """Validated process configuration with secrets hidden from representations."""

    redis_url: str
    redis_prefix: str
    coordinator_origin: str
    mcp_allowed_hosts: tuple[str, ...]
    trusted_proxy_networks: tuple[str, ...]
    herenow_host_suffixes: tuple[str, ...]
    capability_hmac_key: bytes
    rate_limit_hmac_key: bytes
    quota_hmac_key: bytes
    active_aead_key_id: str
    aead_keys: Mapping[str, bytes]
    mcp_max_wait_seconds: int
    renewal_grace_seconds: int
    creation_handoff_seconds: int
    research_deadline_seconds: int
    creating_ttl_seconds: int
    daily_quota_limit: int
    circuit_mode: str
    rate_limits: RateLimitPolicy
    redis_timeout_seconds: int

    def __repr__(self) -> str:
        return (
            "RuntimeSettings(redis_url=<redacted>, redis_prefix=<redacted>, "
            f"coordinator_origin={self.coordinator_origin!r}, mcp_allowed_hosts={self.mcp_allowed_hosts!r}, "
            "trusted_proxy_networks=<redacted>, herenow_host_suffixes=<redacted>, "
            "capability_hmac_key=<redacted>, rate_limit_hmac_key=<redacted>, quota_hmac_key=<redacted>, "
            f"active_aead_key_id={self.active_aead_key_id!r}, aead_keys=<redacted>, "
            f"mcp_max_wait_seconds={self.mcp_max_wait_seconds!r}, renewal_grace_seconds={self.renewal_grace_seconds!r}, "
            f"creation_handoff_seconds={self.creation_handoff_seconds!r}, research_deadline_seconds={self.research_deadline_seconds!r}, "
            f"creating_ttl_seconds={self.creating_ttl_seconds!r}, daily_quota_limit={self.daily_quota_limit!r}, "
            f"circuit_mode={self.circuit_mode!r}, rate_limits={self.rate_limits!r}, redis_timeout_seconds={self.redis_timeout_seconds!r})"
        )

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> "RuntimeSettings":
        values = os.environ if environ is None else environ
        aead_keys = _aead_keys(values)
        active_aead_key_id = _required(values, "WINNOW_REMOTE_ACTIVE_AEAD_KEY_ID")
        if active_aead_key_id not in aead_keys:
            raise RuntimeConfigurationError("WINNOW_REMOTE_ACTIVE_AEAD_KEY_ID is not present in WINNOW_REMOTE_AEAD_KEYS_JSON")
        circuit_mode = values.get("WINNOW_REMOTE_CIRCUIT_MODE", "normal")
        if circuit_mode not in {"normal", "no_new_sessions", "read_only_existing", "status_only"}:
            raise RuntimeConfigurationError("WINNOW_REMOTE_CIRCUIT_MODE is invalid")
        redis_prefix = values.get("WINNOW_REMOTE_REDIS_PREFIX", "winnow:remote:v1")
        if not isinstance(redis_prefix, str) or not redis_prefix or len(redis_prefix) > 128 or any(character.isspace() for character in redis_prefix):
            raise RuntimeConfigurationError("WINNOW_REMOTE_REDIS_PREFIX is invalid")
        return cls(
            redis_url=_redis_url(_required(values, "WINNOW_REMOTE_REDIS_URL")),
            redis_prefix=redis_prefix,
            coordinator_origin=_https_origin(_required(values, "WINNOW_REMOTE_COORDINATOR_ORIGIN"), name="WINNOW_REMOTE_COORDINATOR_ORIGIN"),
            mcp_allowed_hosts=_csv(values, "WINNOW_REMOTE_MCP_ALLOWED_HOSTS", required=True),
            trusted_proxy_networks=_csv(values, "WINNOW_REMOTE_TRUSTED_PROXY_CIDRS"),
            herenow_host_suffixes=_csv(values, "WINNOW_REMOTE_HERENOW_HOST_SUFFIXES") or (".here.now",),
            capability_hmac_key=_base64url_secret(values, "WINNOW_REMOTE_CAPABILITY_HMAC_KEY_B64", lengths=frozenset({32})),
            rate_limit_hmac_key=_base64url_secret(values, "WINNOW_REMOTE_RATE_LIMIT_HMAC_KEY_B64", lengths=frozenset({32})),
            quota_hmac_key=_base64url_secret(values, "WINNOW_REMOTE_QUOTA_HMAC_KEY_B64", lengths=frozenset({32})),
            active_aead_key_id=active_aead_key_id,
            aead_keys=aead_keys,
            mcp_max_wait_seconds=_integer(values, "WINNOW_REMOTE_MAX_WAIT_SECONDS", default=300, minimum=1, maximum=900),
            renewal_grace_seconds=_integer(values, "WINNOW_REMOTE_WAIT_RENEWAL_GRACE_SECONDS", default=15, minimum=0, maximum=300),
            creation_handoff_seconds=_integer(values, "WINNOW_REMOTE_CREATION_HANDOFF_SECONDS", default=300, minimum=1, maximum=3_600),
            research_deadline_seconds=_integer(values, "WINNOW_REMOTE_RESEARCH_DEADLINE_SECONDS", default=1_800, minimum=1, maximum=86_400),
            creating_ttl_seconds=_integer(values, "WINNOW_REMOTE_CREATING_TTL_SECONDS", default=900, minimum=1, maximum=3_600),
            daily_quota_limit=_integer(values, "WINNOW_REMOTE_DAILY_QUOTA_LIMIT", default=10, minimum=1, maximum=100),
            circuit_mode=circuit_mode,
            rate_limits=RateLimitPolicy(
                status=RateLimit(_integer(values, "WINNOW_REMOTE_RATE_STATUS_PER_MINUTE", default=60, minimum=1, maximum=10_000), 60),
                next_round=RateLimit(_integer(values, "WINNOW_REMOTE_RATE_NEXT_ROUND_PER_MINUTE", default=6, minimum=1, maximum=1_000), 60),
                mcp_create=RateLimit(_integer(values, "WINNOW_REMOTE_RATE_MCP_CREATE_PER_MINUTE", default=10, minimum=1, maximum=1_000), 60),
                mcp_publish=RateLimit(_integer(values, "WINNOW_REMOTE_RATE_MCP_PUBLISH_PER_MINUTE", default=6, minimum=1, maximum=1_000), 60),
            ),
            redis_timeout_seconds=_integer(values, "WINNOW_REMOTE_REDIS_TIMEOUT_SECONDS", default=5, minimum=1, maximum=30),
        )


class RedisReadinessProbe:
    """Return only whether the configured Redis endpoint is reachable."""

    def __init__(self, client: Any) -> None:
        self._client = client

    def __call__(self) -> bool:
        try:
            return bool(self._client.ping())
        except Exception:
            return False


def create_application(settings: RuntimeSettings | None = None) -> Starlette:
    """Create the deployed ASGI app; use this as Uvicorn's ``--factory`` target."""

    configured = RuntimeSettings.from_environ() if settings is None else settings
    try:
        import redis
    except ImportError as exc:  # pragma: no cover - packaging failure only
        raise RuntimeError("the production Redis dependency is unavailable") from exc
    client = redis.Redis.from_url(
        configured.redis_url,
        decode_responses=False,
        socket_connect_timeout=configured.redis_timeout_seconds,
        socket_timeout=configured.redis_timeout_seconds,
        health_check_interval=30,
        ssl_cert_reqs="required",
    )
    repository = RedisRepository(client, now=time.time, prefix=configured.redis_prefix)
    security = CapabilitySecurity(
        capability_hmac_key=configured.capability_hmac_key,
        active_key_id=configured.active_aead_key_id,
        aead_keys=configured.aead_keys,
    )
    coordinator = Coordinator(
        repository,
        security,
        config=CoordinatorConfig(
            max_wait_seconds=configured.mcp_max_wait_seconds,
            renewal_grace_seconds=configured.renewal_grace_seconds,
            creation_handoff_seconds=configured.creation_handoff_seconds,
            research_deadline_seconds=configured.research_deadline_seconds,
            creating_ttl_seconds=configured.creating_ttl_seconds,
            daily_quota_limit=configured.daily_quota_limit,
            allowed_herenow_host_suffixes=configured.herenow_host_suffixes,
            quota_hmac_key=configured.quota_hmac_key,
        ),
        circuit_mode=configured.circuit_mode,
    )
    return create_app(
        AppConfig(
            coordinator_origin=configured.coordinator_origin,
            mcp_allowed_hosts=configured.mcp_allowed_hosts,
            trusted_proxy_policy=TrustedProxyPolicy(configured.trusted_proxy_networks),
            browser=BrowserApiConfig(allowed_herenow_host_suffixes=configured.herenow_host_suffixes),
            mcp_max_wait_seconds=configured.mcp_max_wait_seconds,
        ),
        AppDependencies(
            coordinator=coordinator,
            publisher_factory=lambda html_builder: HereNowPublisher(html_builder=html_builder),
            rate_limiter=RateLimiter(repository, hmac_key=configured.rate_limit_hmac_key, policy=configured.rate_limits),
            notifier=RedisWaitNotifier(client, prefix=f"{configured.redis_prefix}:wait"),
            readiness_probe=RedisReadinessProbe(client),
        ),
    )
