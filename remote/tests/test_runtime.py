from __future__ import annotations

import asyncio
import base64
import json
import sys
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "remote" / "src"))

from winnow_remote.app import AppConfig, AppDependencies, create_app  # noqa: E402
from winnow_remote.coordinator import Coordinator, CoordinatorConfig  # noqa: E402
from winnow_remote.repository import FakeRepository  # noqa: E402
from winnow_remote.runtime import RuntimeConfigurationError, RuntimeSettings, create_application  # noqa: E402
from winnow_remote.security import CapabilitySecurity  # noqa: E402
from winnow_remote.settings import PollingWaitNotifier, RateLimiter  # noqa: E402


def secret(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def environment() -> dict[str, str]:
    key = secret(b"k" * 32)
    return {
        "WINNOW_REMOTE_REDIS_URL": "rediss://default:service-password@redis.example.test:6380/0",
        "WINNOW_REMOTE_COORDINATOR_ORIGIN": "https://mcp.example.test/",
        "WINNOW_REMOTE_MCP_ALLOWED_HOSTS": "mcp.example.test",
        "WINNOW_REMOTE_TRUSTED_PROXY_CIDRS": "198.51.100.0/24",
        "WINNOW_REMOTE_CAPABILITY_HMAC_KEY_B64": key,
        "WINNOW_REMOTE_RATE_LIMIT_HMAC_KEY_B64": secret(b"r" * 32),
        "WINNOW_REMOTE_QUOTA_HMAC_KEY_B64": secret(b"q" * 32),
        "WINNOW_REMOTE_ACTIVE_AEAD_KEY_ID": "2026-08",
        "WINNOW_REMOTE_AEAD_KEYS_JSON": json.dumps({"2026-08": secret(b"a" * 32)}),
    }


class RuntimeSettingsTests(unittest.TestCase):
    def test_runtime_settings_are_strict_and_redact_secrets(self):
        values = environment()
        configured = RuntimeSettings.from_environ(values)
        self.assertEqual(configured.coordinator_origin, "https://mcp.example.test")
        self.assertEqual(configured.mcp_allowed_hosts, ("mcp.example.test",))
        self.assertEqual(configured.trusted_proxy_networks, ("198.51.100.0/24",))
        self.assertEqual(configured.circuit_mode, "normal")
        rendered = repr(configured)
        for raw in (
            values["WINNOW_REMOTE_REDIS_URL"],
            values["WINNOW_REMOTE_CAPABILITY_HMAC_KEY_B64"],
            values["WINNOW_REMOTE_RATE_LIMIT_HMAC_KEY_B64"],
            values["WINNOW_REMOTE_QUOTA_HMAC_KEY_B64"],
            values["WINNOW_REMOTE_AEAD_KEYS_JSON"],
        ):
            self.assertNotIn(raw, rendered)

    def test_runtime_settings_reject_insecure_or_inconsistent_configuration(self):
        values = environment()
        values["WINNOW_REMOTE_REDIS_URL"] = "redis://redis.example.test/0"
        with self.assertRaises(RuntimeConfigurationError):
            RuntimeSettings.from_environ(values)

        values = environment()
        values["WINNOW_REMOTE_ACTIVE_AEAD_KEY_ID"] = "missing"
        with self.assertRaises(RuntimeConfigurationError):
            RuntimeSettings.from_environ(values)

        values = environment()
        values["WINNOW_REMOTE_COORDINATOR_ORIGIN"] = "https://mcp.example.test/not-an-origin"
        with self.assertRaises(RuntimeConfigurationError):
            RuntimeSettings.from_environ(values)


class OperationalEndpointTests(unittest.TestCase):
    def make_app(self, ready: bool):
        now = time.time
        repository = FakeRepository(now=now)
        security = CapabilitySecurity(
            capability_hmac_key=b"c" * 32,
            active_key_id="current",
            aead_keys={"current": b"a" * 32},
        )
        coordinator = Coordinator(
            repository,
            security,
            config=CoordinatorConfig(quota_hmac_key=b"q" * 32),
        )
        return create_app(
            AppConfig(coordinator_origin="https://mcp.example.test", mcp_allowed_hosts=("mcp.example.test",)),
            AppDependencies(
                coordinator=coordinator,
                publisher_factory=lambda _builder: None,
                rate_limiter=RateLimiter(repository, hmac_key=b"r" * 32),
                notifier=PollingWaitNotifier(),
                readiness_probe=lambda: ready,
            ),
        )

    @staticmethod
    def response(app, path: str):
        route = next(item for item in app.routes if getattr(item, "path", None) == path)
        return asyncio.run(route.endpoint(None))

    def test_health_and_readiness_are_constant_and_have_no_session_state(self):
        healthy = self.make_app(True)
        health = self.response(healthy, "/healthz")
        ready = self.response(healthy, "/readyz")
        self.assertEqual((health.status_code, health.body), (200, b'{"status":"ok"}'))
        self.assertEqual((ready.status_code, ready.body), (200, b'{"status":"ready"}'))
        self.assertEqual(health.headers["cache-control"], "no-store")
        self.assertEqual(ready.headers["cache-control"], "no-store")

        unavailable = self.response(self.make_app(False), "/readyz")
        self.assertEqual((unavailable.status_code, unavailable.body), (503, b'{"status":"unavailable"}'))
        self.assertNotIn(b"session", unavailable.body)

    def test_production_factory_uses_tls_redis_and_readiness_stays_opaque(self):
        configured = RuntimeSettings.from_environ(environment())
        calls = []

        class Client:
            def ping(self):
                return False

        class RedisFactory:
            @staticmethod
            def from_url(*args, **kwargs):
                calls.append((args, kwargs))
                return Client()

        with patch.dict(sys.modules, {"redis": SimpleNamespace(Redis=RedisFactory)}):
            app = create_application(configured)
        self.assertEqual(calls[0][0], (configured.redis_url,))
        self.assertEqual(calls[0][1]["ssl_cert_reqs"], "required")
        response = self.response(app, "/readyz")
        self.assertEqual((response.status_code, response.body), (503, b'{"status":"unavailable"}'))
