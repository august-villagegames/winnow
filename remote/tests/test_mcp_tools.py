from __future__ import annotations

import asyncio
import base64
import json
import sys
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "remote" / "src"))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from winnow_remote.app import AppConfig, AppDependencies, create_app  # noqa: E402
from winnow_remote.contracts import BrowserNextRoundRequest  # noqa: E402
from winnow_remote.coordinator import Coordinator, CoordinatorConfig  # noqa: E402
from winnow_remote.mcp_tools import McpToolConfig, McpToolService, register_mcp_tools  # noqa: E402
from winnow_remote.repository import FakeRepository  # noqa: E402
from winnow_remote.security import CapabilitySecurity  # noqa: E402
from winnow_remote.settings import (  # noqa: E402
    PollingWaitNotifier,
    RateLimiter,
    RequestProvenance,
    _CURRENT_MCP_PROVENANCE,
)


def fixture(name: str):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


class FakePublisher:
    def __init__(self, html_builder, built):
        self._html_builder = html_builder
        self._built = built

    def create(self, seed, *, persist_claim, published_revision, expected_markers):
        self._built.append(self._html_builder(seed, None))
        created = SimpleNamespace(
            site_url="https://demo.here.now/",
            slug="demo",
            expires_at="2026-08-19T12:00:00Z",
            claim_token="claim-token",
        )
        persist_claim(created)
        self._built.append(self._html_builder(seed, created.expires_at))
        return SimpleNamespace(created=created, pending_version=None)

    def update(
        self,
        seed,
        *,
        slug,
        claim_token,
        site_url,
        original_expires_at,
        persist_pending,
        published_revision,
        expected_markers,
    ):
        self._built.append(self._html_builder(seed, original_expires_at))
        persist_pending(
            SimpleNamespace(
                slug=slug,
                version_id="version-2",
                published_revision=published_revision,
                expected_markers=expected_markers,
            )
        )
        return SimpleNamespace()


class McpToolTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
        self.timestamp = lambda: self.now.timestamp()
        self.security = CapabilitySecurity(capability_hmac_key=b"h" * 32, active_key_id="current", aead_keys={"current": b"c" * 32})
        self.repository = FakeRepository(now=self.timestamp)
        self.coordinator = Coordinator(
            self.repository,
            self.security,
            config=CoordinatorConfig(max_wait_seconds=2, renewal_grace_seconds=2, quota_hmac_key=b"q" * 32),
            now=lambda: self.now,
        )
        self.built = []
        self.rate_limiter = RateLimiter(self.repository, hmac_key=b"r" * 32, now=self.timestamp)
        self.service = McpToolService(
            self.coordinator,
            publisher_factory=lambda builder: FakePublisher(builder, self.built),
            rate_limiter=self.rate_limiter,
            notifier=PollingWaitNotifier(),
            config=McpToolConfig(coordinator_origin="https://coordinator.example/", max_wait_seconds=2, wait_poll_seconds=0.5),
        )
        self.seed = fixture("synthetic-seed.json")

    def with_provenance(self, coroutine):
        token = _CURRENT_MCP_PROVENANCE.set(RequestProvenance(network_prefix="203.0.113.0/24", client_family="anthropic"))
        try:
            return asyncio.run(coroutine)
        finally:
            _CURRENT_MCP_PROVENANCE.reset(token)

    def create(self):
        result = self.with_provenance(self.service.create({"seed": self.seed, "mode": "rolling"}))
        self.assertEqual(result["status"], "awaiting_agent_wait")
        return result

    @staticmethod
    def page_envelope(html):
        marker = b'<script id="winnow-rolling-page" type="application/octet-stream">'
        encoded = html.split(marker, 1)[1].split(b"</script>", 1)[0]
        return json.loads(base64.b64decode(encoded).decode("utf-8"))

    def browser_request(self, receipt):
        return BrowserNextRoundRequest.parse(
            {
                "protocol": "winnow.browser-request",
                "version": 1,
                "idempotencyKey": str(uuid.uuid4()),
                "roundNumber": 1,
                "seedHash": receipt["seedHash"],
                "publishedRevision": 1,
                "verdicts": [
                    {"optionId": option["id"], "decision": decision}
                    for option, decision in zip(self.seed["round"]["options"], ["like", "dislike", "like", "dislike", "like", "skip"])
                ],
                "selectedProfileKeys": [fixture("synthetic-continuation.json")["profilePatterns"][0]["key"]],
            }
        )

    def test_official_sdk_registers_exactly_the_three_structured_tools(self):
        server = MCPServer(name="test")
        register_mcp_tools(server, self.service)
        tools = asyncio.run(server.list_tools())
        self.assertEqual([tool.name for tool in tools], ["create_winnow_session", "wait_for_continue", "publish_next_round"])
        for tool in tools:
            self.assertFalse(tool.input_schema.get("additionalProperties", True))
        create = tools[0]
        self.assertEqual(set(create.input_schema["properties"]), {"seed", "mode"})

    def test_streamable_http_mcp_lists_tools_and_rejects_unknown_tool_arguments_before_sdk_decode(self):
        app = create_app(
            AppConfig(coordinator_origin="https://coordinator.example/", mcp_allowed_hosts=("testserver",)),
            AppDependencies(
                coordinator=self.coordinator,
                publisher_factory=lambda builder: FakePublisher(builder, self.built),
                rate_limiter=self.rate_limiter,
                notifier=PollingWaitNotifier(),
            ),
        )

        async def request(payload):
            body = json.dumps(payload).encode()
            scope = {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "https",
                "path": "/mcp",
                "raw_path": b"/mcp",
                "query_string": b"",
                "headers": [
                    (b"host", b"testserver"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                    (b"accept", b"application/json"),
                ],
                "client": ("203.0.113.7", 1234),
                "server": ("testserver", 443),
            }
            incoming = [{"type": "http.request", "body": body, "more_body": False}]
            sent = []

            async def receive():
                return incoming.pop(0) if incoming else {"type": "http.disconnect"}

            async def send(message):
                sent.append(message)

            await app(scope, receive, send)
            status = next(item["status"] for item in sent if item["type"] == "http.response.start")
            body = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
            return int(status), json.loads(body) if body else None

        async def scenario():
            async with app.router.lifespan_context(app):
                initialized = await request(
                    {
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "initialize",
                        "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}},
                    }
                )
                listed = await request({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
                created = await request(
                    {
                        "jsonrpc": "2.0",
                        "id": 3,
                        "method": "tools/call",
                        "params": {"name": "create_winnow_session", "arguments": {"seed": self.seed, "mode": "rolling"}},
                    }
                )
                rejected = await request(
                    {
                        "jsonrpc": "2.0",
                        "id": 4,
                        "method": "tools/call",
                        "params": {
                            "name": "wait_for_continue",
                            "arguments": {"sessionHandle": "x", "expectedRoundNumber": 1, "expectedSeedHash": "a" * 64, "maxWaitSeconds": 1, "unknown": True},
                        },
                    }
                )
                return initialized, listed, created, rejected

        initialized, listed, created, rejected = asyncio.run(scenario())
        self.assertEqual(initialized[0], 200)
        self.assertEqual(listed[0], 200)
        self.assertEqual([tool["name"] for tool in listed[1]["result"]["tools"]], ["create_winnow_session", "wait_for_continue", "publish_next_round"])
        receipt = created[1]["result"]["structuredContent"]
        self.assertEqual((created[0], receipt["status"]), (200, "awaiting_agent_wait"))
        self.assertNotIn("claim-token", json.dumps(created[1]))
        self.assertEqual(rejected, (400, {"error": "request_rejected"}))

    def test_create_derives_browser_bearer_without_durability_and_successor_reuses_it(self):
        receipt = self.create()
        stored = self.repository.lookup_agent(self.security.capability_hash(receipt["sessionHandle"]))
        self.assertIsNotNone(stored)
        browser = self.security.browser_capability_for_session(stored.session_id)
        self.assertNotEqual(browser, receipt["sessionHandle"])
        self.assertEqual(self.page_envelope(self.built[-1])["browserCapability"], browser)
        encoded_record = json.dumps(stored.as_dict())
        self.assertNotIn(browser, encoded_record)
        self.assertNotIn(receipt["sessionHandle"], encoded_record)
        other = self.security.browser_capability_for_session("different-high-entropy-session-id")
        self.assertNotEqual(browser, other)
        self.assertEqual(len(base64.urlsafe_b64decode(browser + "=" * (-len(browser) % 4))), 32)

        wait = self.with_provenance(
            self.service.wait(
                {
                    "sessionHandle": receipt["sessionHandle"],
                    "expectedRoundNumber": 1,
                    "expectedSeedHash": receipt["seedHash"],
                    "maxWaitSeconds": 1,
                }
            )
        )
        self.assertEqual(wait["status"], "still_waiting")
        accepted = self.coordinator.accept_browser_next_round(browser, origin="https://demo.here.now", request=self.browser_request(receipt))
        self.assertEqual(accepted["status"], "accepted")
        event = self.with_provenance(
            self.service.wait(
                {
                    "sessionHandle": receipt["sessionHandle"],
                    "expectedRoundNumber": 1,
                    "expectedSeedHash": receipt["seedHash"],
                    "maxWaitSeconds": 1,
                }
            )
        )
        successor = fixture("synthetic-successor-seed.json")
        published = self.with_provenance(
            self.service.publish(
                {
                    "sessionHandle": receipt["sessionHandle"],
                    "eventId": event["eventId"],
                    "publishFence": event["publishFence"],
                    "parentSeedHash": receipt["seedHash"],
                    "nextSeed": successor,
                }
            )
        )
        self.assertEqual((published["roundNumber"], published["publishedRevision"]), (2, 2))
        self.assertEqual(self.page_envelope(self.built[-1])["browserCapability"], browser)

    def test_cancelling_one_wait_after_event_acceptance_never_loses_the_durable_event(self):
        receipt = self.create()
        stored = self.repository.lookup_agent(self.security.capability_hash(receipt["sessionHandle"]))
        browser = self.security.browser_capability_for_session(stored.session_id)

        async def cancelled_wait():
            task = asyncio.create_task(
                self.service.wait(
                    {
                        "sessionHandle": receipt["sessionHandle"],
                        "expectedRoundNumber": 1,
                        "expectedSeedHash": receipt["seedHash"],
                        "maxWaitSeconds": 2,
                    }
                )
            )
            await asyncio.sleep(0.05)
            self.coordinator.accept_browser_next_round(browser, origin="https://demo.here.now", request=self.browser_request(receipt))
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

        self.with_provenance(cancelled_wait())
        redelivered = self.with_provenance(
            self.service.wait(
                {
                    "sessionHandle": receipt["sessionHandle"],
                    "expectedRoundNumber": 1,
                    "expectedSeedHash": receipt["seedHash"],
                    "maxWaitSeconds": 1,
                }
            )
        )
        self.assertEqual(redelivered["status"], "continue_requested")
        self.assertIn("eventId", redelivered)
        self.assertNotIn(browser, json.dumps(redelivered))


if __name__ == "__main__":
    unittest.main()
