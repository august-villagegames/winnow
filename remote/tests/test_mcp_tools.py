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

from jsonschema import Draft202012Validator


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "remote" / "src"))

from mcp.server.mcpserver import MCPServer  # noqa: E402

from winnow_remote.app import AppConfig, AppDependencies, create_app  # noqa: E402
from winnow_remote.contracts import BrowserNextRoundRequest  # noqa: E402
from winnow_remote.coordinator import Coordinator, CoordinatorConfig  # noqa: E402
from winnow_remote.herenow import MAX_REMOTE_MCP_RESULT_BYTES  # noqa: E402
from winnow_remote.mcp_contract import (  # noqa: E402
    ROUND_ONE_AUTHORING_GUIDE_RESOURCE_URI,
    ROUND_ONE_EXAMPLE,
    SEED_SCHEMA_RESOURCE_URI,
    canonical_seed_schema_bytes,
    canonical_seed_schema_path,
    round_one_authoring_guide,
    seed_contract_payload,
)
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

    def test_official_sdk_registers_contract_resources_and_four_closed_tools(self):
        server = MCPServer(name="test")
        register_mcp_tools(server, self.service)
        tools = asyncio.run(server.list_tools())
        self.assertEqual(
            [tool.name for tool in tools],
            ["get_winnow_v4_seed_contract", "create_winnow_session", "wait_for_continue", "publish_next_round"],
        )
        for tool in tools:
            self.assertFalse(tool.input_schema.get("additionalProperties", True))
        contract = tools[0]
        self.assertEqual(contract.input_schema["properties"], {})
        self.assertEqual(
            contract.annotations.model_dump(by_alias=True, exclude_none=True),
            {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        )
        create = tools[1]
        self.assertEqual(set(create.input_schema["properties"]), {"seed", "mode"})
        mode = create.input_schema["properties"]["mode"]
        self.assertEqual(mode["const"], "rolling")
        self.assertEqual(mode["type"], "string")
        self.assertIn("get_winnow_v4_seed_contract", create.description)
        self.assertIn(SEED_SCHEMA_RESOURCE_URI, create.description)

        resources = asyncio.run(server.list_resources())
        self.assertEqual([str(resource.uri) for resource in resources], [SEED_SCHEMA_RESOURCE_URI, ROUND_ONE_AUTHORING_GUIDE_RESOURCE_URI])
        self.assertEqual([resource.mime_type for resource in resources], ["application/schema+json", "text/markdown"])

    def test_contract_resources_and_fallback_tool_are_fixed_valid_and_redacted(self):
        server = MCPServer(name="test")
        register_mcp_tools(server, self.service)

        schema_resource = asyncio.run(server.read_resource(SEED_SCHEMA_RESOURCE_URI))[0]
        guide_resource = asyncio.run(server.read_resource(ROUND_ONE_AUTHORING_GUIDE_RESOURCE_URI))[0]
        self.assertEqual(schema_resource.content.encode("utf-8"), canonical_seed_schema_bytes())
        self.assertEqual(canonical_seed_schema_path().read_bytes(), canonical_seed_schema_bytes())
        self.assertEqual(guide_resource.content, round_one_authoring_guide())

        # The portable core is the runtime validation boundary. The fixed guide
        # example must remain a valid text-only round-one seed without becoming
        # a second hand-maintained contract.
        schema = json.loads(canonical_seed_schema_bytes())
        Draft202012Validator(schema).validate(ROUND_ONE_EXAMPLE)
        self.service._core.validate_seed(ROUND_ONE_EXAMPLE)
        self.assertEqual(ROUND_ONE_EXAMPLE["session"]["imagePolicy"]["mode"], "notApplicable")
        self.assertIn("illustrative only", guide_resource.content)
        self.assertIn("do not publish unchanged", guide_resource.content)

        result = asyncio.run(server.call_tool("get_winnow_v4_seed_contract", {}))
        self.assertFalse(result.is_error)
        self.assertEqual(len(result.content), 1)
        payload = json.loads(result.content[0].text)
        self.assertEqual(payload, seed_contract_payload())
        self.assertEqual(payload["schemaResourceUri"], SEED_SCHEMA_RESOURCE_URI)
        self.assertEqual(payload["authoringGuideResourceUri"], ROUND_ONE_AUTHORING_GUIDE_RESOURCE_URI)
        self.assertEqual(payload["seedSchema"], json.loads(canonical_seed_schema_bytes()))
        self.assertEqual(payload["roundOneAuthoringGuide"], round_one_authoring_guide())

        encoded_result = json.dumps(
            {"content": [item.model_dump(by_alias=True, exclude_none=True) for item in result.content]},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        self.assertLessEqual(len(encoded_result), MAX_REMOTE_MCP_RESULT_BYTES)
        serialized = json.dumps({"resources": [payload, guide_resource.content]}, ensure_ascii=False)
        for forbidden in ("sessionHandle", "browserCapability", "claimToken", "publishFence", "eventId", "agentLease"):
            self.assertNotIn(forbidden, serialized)
        self.assertNotIn(str(canonical_seed_schema_path()), serialized)

        with self.assertRaisesRegex(Exception, "resource"):
            asyncio.run(server.read_resource("winnow://contracts/v4/not-found"))

    def test_docker_runtime_recipe_packages_the_canonical_schema_source(self):
        dockerfile = (ROOT / "remote" / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn(
            "COPY --chown=winnow:winnow .agents/skills/winnow/references/seed.schema.json .agents/skills/winnow/references/seed.schema.json",
            dockerfile,
        )

    def test_create_reports_fixed_contract_guidance_without_echoing_input(self):
        wrong_mode = self.with_provenance(self.service.create({"seed": self.seed, "mode": "publish"}))
        self.assertEqual(
            wrong_mode,
            {
                "status": "rejected",
                "reason": "invalid_mode",
                "message": "mode must be the literal string 'rolling'.",
            },
        )

        invalid_seed = self.with_provenance(self.service.create({"seed": {"private": "do not echo this"}, "mode": "rolling"}))
        self.assertEqual(
            invalid_seed,
            {
                "status": "rejected",
                "reason": "invalid_seed",
                "message": "The seed must be a valid Winnow v4 round-one seed.",
            },
        )
        self.assertNotIn("private", json.dumps(invalid_seed))

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
                resources = await request({"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}})
                schema = await request(
                    {"jsonrpc": "2.0", "id": 4, "method": "resources/read", "params": {"uri": SEED_SCHEMA_RESOURCE_URI}}
                )
                guide = await request(
                    {"jsonrpc": "2.0", "id": 5, "method": "resources/read", "params": {"uri": ROUND_ONE_AUTHORING_GUIDE_RESOURCE_URI}}
                )
                missing_resource = await request(
                    {"jsonrpc": "2.0", "id": 6, "method": "resources/read", "params": {"uri": "winnow://contracts/v4/not-found"}}
                )
                contract = await request(
                    {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "get_winnow_v4_seed_contract", "arguments": {}}}
                )
                contract_without_arguments = await request(
                    {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "get_winnow_v4_seed_contract"}}
                )
                contract_rejected = await request(
                    {
                        "jsonrpc": "2.0",
                        "id": 9,
                        "method": "tools/call",
                        "params": {"name": "get_winnow_v4_seed_contract", "arguments": {"unexpected": True}},
                    }
                )
                created = await request(
                    {
                        "jsonrpc": "2.0",
                        "id": 10,
                        "method": "tools/call",
                        "params": {"name": "create_winnow_session", "arguments": {"seed": self.seed, "mode": "rolling"}},
                    }
                )
                rejected = await request(
                    {
                        "jsonrpc": "2.0",
                        "id": 11,
                        "method": "tools/call",
                        "params": {
                            "name": "wait_for_continue",
                            "arguments": {"sessionHandle": "x", "expectedRoundNumber": 1, "expectedSeedHash": "a" * 64, "maxWaitSeconds": 1, "unknown": True},
                        },
                    }
                )
                return initialized, listed, resources, schema, guide, missing_resource, contract, contract_without_arguments, contract_rejected, created, rejected

        initialized, listed, resources, schema, guide, missing_resource, contract, contract_without_arguments, contract_rejected, created, rejected = asyncio.run(scenario())
        self.assertEqual(initialized[0], 200)
        self.assertEqual(listed[0], 200)
        self.assertEqual(
            [tool["name"] for tool in listed[1]["result"]["tools"]],
            ["get_winnow_v4_seed_contract", "create_winnow_session", "wait_for_continue", "publish_next_round"],
        )
        contract_schema = listed[1]["result"]["tools"][0]
        self.assertEqual(contract_schema["inputSchema"]["properties"], {})
        self.assertEqual(
            contract_schema["annotations"],
            {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True, "openWorldHint": False},
        )
        self.assertEqual([resource["uri"] for resource in resources[1]["result"]["resources"]], [SEED_SCHEMA_RESOURCE_URI, ROUND_ONE_AUTHORING_GUIDE_RESOURCE_URI])
        self.assertEqual(schema[1]["result"]["contents"][0]["text"].encode("utf-8"), canonical_seed_schema_bytes())
        self.assertEqual(guide[1]["result"]["contents"][0]["text"], round_one_authoring_guide())
        self.assertEqual(missing_resource[1]["error"]["code"], -32602)
        self.assertNotIn(str(canonical_seed_schema_path()), json.dumps(missing_resource[1]))
        contract_payload = json.loads(contract[1]["result"]["content"][0]["text"])
        self.assertEqual(contract_payload, seed_contract_payload())
        self.assertEqual(json.loads(contract_without_arguments[1]["result"]["content"][0]["text"]), seed_contract_payload())
        self.assertLessEqual(len(json.dumps(contract[1], ensure_ascii=False).encode("utf-8")), MAX_REMOTE_MCP_RESULT_BYTES)
        self.assertEqual(contract_rejected, (400, {"error": "request_rejected"}))
        receipt = created[1]["result"]["structuredContent"]
        self.assertEqual((created[0], receipt["status"]), (200, "awaiting_agent_wait"))
        content = created[1]["result"]["content"]
        self.assertEqual(content[0], {"type": "resource_link", "name": "Winnow session", "uri": receipt["siteUrl"], "mimeType": "text/html"})
        self.assertEqual(content[1]["annotations"], {"audience": ["assistant"]})
        handoff = json.loads(content[1]["text"])
        self.assertEqual(
            handoff,
            {
                "nextTool": "wait_for_continue",
                "arguments": {
                    "sessionHandle": receipt["sessionHandle"],
                    "expectedRoundNumber": receipt["roundNumber"],
                    "expectedSeedHash": receipt["seedHash"],
                    "maxWaitSeconds": 300,
                },
            },
        )
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
