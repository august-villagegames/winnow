from __future__ import annotations

import asyncio
import json
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "remote" / "src"))

from winnow_remote.browser_api import BrowserApi  # noqa: E402
from winnow_remote.contracts import BrowserNextRoundRequest  # noqa: E402
from winnow_remote.coordinator import Coordinator, CoordinatorConfig  # noqa: E402
from winnow_remote.repository import FakeRepository  # noqa: E402
from winnow_remote.security import CapabilitySecurity  # noqa: E402
from winnow_remote.settings import InProcessWaitNotifier, RateLimiter, TrustedProxyPolicy  # noqa: E402


def fixture(name: str = "synthetic-seed.json"):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


class BrowserApiTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
        self.timestamp = lambda: self.now.timestamp()
        self.security = CapabilitySecurity(capability_hmac_key=b"h" * 32, active_key_id="current", aead_keys={"current": b"c" * 32})
        self.repository = FakeRepository(now=self.timestamp)
        self.coordinator = Coordinator(
            self.repository,
            self.security,
            config=CoordinatorConfig(max_wait_seconds=10, quota_hmac_key=b"q" * 32),
            now=lambda: self.now,
        )
        self.rate_limiter = RateLimiter(self.repository, hmac_key=b"r" * 32, now=self.timestamp)
        self.notifier = InProcessWaitNotifier()
        self.api = BrowserApi(self.coordinator, rate_limiter=self.rate_limiter, notifier=self.notifier)
        self.seed = fixture()
        self.handle = self.coordinator.begin_creation(self.seed, network_prefix="203.0.113.0/24", client_family="anthropic")
        self.coordinator.persist_creation_publication(
            self.handle,
            site_url="https://demo.here.now/",
            slug="demo",
            original_expires_at="2026-08-19T12:00:00Z",
            claim_token="claim-token",
        )
        self.receipt = self.coordinator.activate_creation(self.handle)

    def request(self, endpoint, *, method="GET", query="", body=b"", headers=None):
        headers = [(b"host", b"testserver")]
        for key, value in (headers or {}).items():
            headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": endpoint,
            "raw_path": endpoint.encode(),
            "query_string": query.encode(),
            "headers": headers,
            "client": ("203.0.113.7", 1234),
            "server": ("testserver", 443),
        }
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        sent = []

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        asyncio.run(endpoint(scope, receive, send) if callable(endpoint) and not hasattr(endpoint, "__self__") else endpoint)
        # Starlette endpoints are functions taking a Request in normal use.
        return sent

    def call(self, handler, *, method="GET", query="", body=b"", headers=None):
        all_headers = [(b"host", b"testserver")]
        for key, value in (headers or {}).items():
            all_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
        from starlette.requests import Request

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": "/v1/session/status",
            "raw_path": b"/v1/session/status",
            "query_string": query.encode(),
            "headers": all_headers,
            "client": ("203.0.113.7", 1234),
            "server": ("testserver", 443),
        }
        messages = [{"type": "http.request", "body": body, "more_body": False}]
        sent = []

        async def receive():
            return messages.pop(0) if messages else {"type": "http.disconnect"}

        async def send(message):
            sent.append(message)

        response = asyncio.run(handler(Request(scope, receive)))
        asyncio.run(response(scope, receive, send))
        start = next(item for item in sent if item["type"] == "http.response.start")
        raw = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        return start["status"], {key.decode().lower(): value.decode() for key, value in start["headers"]}, json.loads(raw) if raw else None

    def auth_headers(self, **extra):
        return {
            "origin": "https://demo.here.now",
            "authorization": f"Bearer {self.handle.browser_capability}",
            **extra,
        }

    def browser_request(self, *, extra=None):
        payload = {
            "protocol": "winnow.browser-request",
            "version": 1,
            "idempotencyKey": str(uuid.uuid4()),
            "roundNumber": 1,
            "seedHash": self.receipt["seedHash"],
            "publishedRevision": 1,
            "verdicts": [
                {"optionId": option["id"], "decision": "like" if index % 2 == 0 else "dislike"}
                for index, option in enumerate(self.seed["round"]["options"])
            ],
            "selectedProfileKeys": [],
        }
        payload.update(extra or {})
        return json.dumps(payload).encode()

    def test_status_has_exact_cors_no_store_and_no_capability_or_internal_fields(self):
        self.coordinator.wait_for_continue(
            self.handle.agent_capability,
            type("Wait", (), {"expected_round_number": 1, "expected_seed_hash": self.receipt["seedHash"], "max_wait_seconds": 10})(),
        )
        status, headers, body = self.call(
            self.api.status,
            query=f"roundNumber=1&seedHash={self.receipt['seedHash']}&publishedRevision=1",
            headers=self.auth_headers(),
        )
        self.assertEqual((status, body["status"]), (200, "connected"))
        self.assertEqual(headers["access-control-allow-origin"], "https://demo.here.now")
        self.assertEqual(headers["cache-control"], "no-store")
        serialized = json.dumps(body)
        for secret in (self.handle.browser_capability, self.handle.agent_capability, self.handle.session_id, "claim-token"):
            self.assertNotIn(secret, serialized)
        self.assertEqual(set(body), {"status", "roundNumber", "seedHash", "publishedRevision", "expiresAt", "agentLeaseExpiresAt", "remainingOptionCapacity"})

    def test_preflight_is_syntactic_and_authorized_post_rejects_unknown_legacy_fields(self):
        status, headers, body = self.call(
            self.api.next_round,
            method="OPTIONS",
            headers={
                "origin": "https://future.here.now",
                "access-control-request-method": "POST",
                "access-control-request-headers": "Authorization, Content-Type",
            },
        )
        self.assertEqual((status, body), (204, None))
        self.assertEqual(headers["access-control-allow-origin"], "https://future.here.now")
        self.coordinator.wait_for_continue(
            self.handle.agent_capability,
            type("Wait", (), {"expected_round_number": 1, "expected_seed_hash": self.receipt["seedHash"], "max_wait_seconds": 10})(),
        )
        status, headers, body = self.call(
            self.api.next_round,
            method="POST",
            body=self.browser_request(extra={"continuation": fixture("synthetic-continuation.json")}),
            headers=self.auth_headers(**{"content-type": "application/json"}),
        )
        self.assertEqual((status, body), (400, {"error": "request_rejected"}))
        self.assertEqual(headers["access-control-allow-origin"], "https://demo.here.now")
        wait = self.coordinator.wait_for_continue(
            self.handle.agent_capability,
            type("Wait", (), {"expected_round_number": 1, "expected_seed_hash": self.receipt["seedHash"], "max_wait_seconds": 10})(),
        )
        self.assertEqual(wait["status"], "still_waiting")

    def test_next_round_requires_content_type_and_never_puts_capability_in_url_or_response(self):
        status, headers, body = self.call(
            self.api.next_round,
            method="POST",
            body=b"{}",
            headers=self.auth_headers(),
        )
        self.assertEqual((status, body), (400, {"error": "request_rejected"}))
        self.assertEqual(headers["access-control-allow-origin"], "https://demo.here.now")
        self.assertNotIn(self.handle.browser_capability, json.dumps(body))

    def test_trusted_proxy_policy_ignores_forwarded_values_from_untrusted_peers(self):
        policy = TrustedProxyPolicy(trusted_proxy_networks=("10.0.0.0/8",))
        untrusted = policy.provenance({"client": ("203.0.113.77", 443), "headers": [(b"x-forwarded-for", b"198.51.100.9"), (b"user-agent", b"Claude") ]})
        trusted = policy.provenance({"client": ("10.0.0.8", 443), "headers": [(b"x-forwarded-for", b"198.51.100.9"), (b"user-agent", b"Claude") ]})
        self.assertEqual((untrusted.network_prefix, untrusted.client_family), ("203.0.113.0/24", "anthropic"))
        self.assertEqual((trusted.network_prefix, trusted.client_family), ("198.51.100.0/24", "anthropic"))


if __name__ == "__main__":
    unittest.main()
