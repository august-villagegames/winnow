"""Deterministic local integration coverage for the remote rolling loop.

This module deliberately runs the production ASGI composition, coordinator,
``RedisRepository`` Lua-call boundary, rolling compiler, and ``HereNowPublisher``
together.  Network and provider behavior remain local and bounded: the fake
HereNow service models only create/update/upload/finalize/public-marker reads.
Its Redis client accepts only the exact production scripts, applies Redis TTL
semantics atomically, and records every evaluated script.  That gives the test
the same CAS/TTL transaction surface without requiring a local Redis daemon.
"""

from __future__ import annotations

import asyncio
import base64
import concurrent.futures
import copy
import json
import re
import sys
import threading
import unittest
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "remote" / "src"))

from winnow_remote.app import AppConfig, AppDependencies, create_app  # noqa: E402
from winnow_remote.contracts import BrowserNextRoundRequest, PublishNextRoundRequest, WaitForContinueRequest  # noqa: E402
from winnow_remote.coordinator import Coordinator, CoordinatorConfig, StateConflict, _load_portable_core  # noqa: E402
from winnow_remote.herenow import HERE_NOW_CONTENT_TYPE, HereNowError, HereNowPublisher, MAX_REMOTE_BROWSER_REQUEST_BYTES  # noqa: E402
from winnow_remote.mcp_tools import McpToolConfig, McpToolService  # noqa: E402
from winnow_remote.repository import ActiveSession, RedisRepository  # noqa: E402
from winnow_remote.security import CapabilitySecurity  # noqa: E402
from winnow_remote.settings import InProcessWaitNotifier, RateLimiter, RequestProvenance, _CURRENT_MCP_PROVENANCE  # noqa: E402


def fixture(name: str) -> dict[str, Any]:
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


class Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self.value

    def timestamp(self) -> float:
        return self.value.timestamp()

    def advance(self, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


class RedisScriptHarness:
    """A deterministic Redis-compatible evaluator for the production scripts.

    It intentionally refuses arbitrary Lua.  The repository must pass the
    exact script constants from ``RedisRepository``; the evaluator then models
    the corresponding Redis GET/SET/INCR/EXPIRE atomic transaction and TTL
    behavior under one lock.
    """

    def __init__(self, clock: Clock) -> None:
        self._clock = clock
        self._values: dict[str, tuple[str, float]] = {}
        self._lock = threading.RLock()
        self.evaluated_scripts: list[str] = []

    def get(self, key: str) -> str | None:
        with self._lock:
            self._purge()
            value = self._values.get(key)
            return value[0] if value is not None else None

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> int:
        with self._lock:
            self._purge()
            self.evaluated_scripts.append(script)
            if script == RedisRepository._CREATE:
                self._require(numkeys == 3 and len(keys_and_args) == 6)
                record_key, browser_key, agent_key, encoded, ttl, session_id = keys_and_args
                if self._exists(record_key) or self._exists(browser_key) or self._exists(agent_key):
                    return 0
                expires_at = self._expires(ttl)
                self._values[str(record_key)] = (str(encoded), expires_at)
                self._values[str(browser_key)] = (str(session_id), expires_at)
                self._values[str(agent_key)] = (str(session_id), expires_at)
                return 1
            if script == RedisRepository._CAS:
                self._require(numkeys == 3 and len(keys_and_args) == 7)
                record_key, browser_key, agent_key, expected_version, encoded, ttl, session_id = keys_and_args
                current = self._values.get(str(record_key))
                if current is None:
                    return 0
                if json.loads(current[0])["record_version"] != int(expected_version):
                    return 0
                expires_at = self._expires(ttl)
                self._values[str(record_key)] = (str(encoded), expires_at)
                self._values[str(browser_key)] = (str(session_id), expires_at)
                self._values[str(agent_key)] = (str(session_id), expires_at)
                return 1
            if script == RedisRepository._QUOTA:
                self._require(numkeys == 1 and len(keys_and_args) == 3)
                key, limit, ttl = keys_and_args
                current = int(self._values.get(str(key), ("0", 0))[0])
                if current >= int(limit):
                    return 0
                self._values[str(key)] = (str(current + 1), self._expires(ttl))
                return 1
            raise AssertionError("RedisRepository attempted an unknown transaction script")

    def _exists(self, key: Any) -> bool:
        return str(key) in self._values

    def _expires(self, ttl: Any) -> float:
        if int(ttl) < 1:
            raise AssertionError("RedisRepository passed a non-positive TTL")
        return self._clock.timestamp() + int(ttl)

    def _purge(self) -> None:
        now = self._clock.timestamp()
        for key, (_value, expires_at) in tuple(self._values.items()):
            if expires_at <= now:
                self._values.pop(key, None)

    @staticmethod
    def _require(condition: bool) -> None:
        if not condition:
            raise AssertionError("RedisRepository called a production script with invalid arguments")


class SimulatedProcessExit(BaseException):
    """Represents a worker loss that must not be converted into a tool reply."""


@dataclass
class ProviderVersion:
    slug: str
    version_id: str
    upload_url: str
    finalize_url: str
    html: bytes | None = None


class FakeHereNow:
    """Deterministic HereNow create/update fake with public-read convergence."""

    endpoint = "https://provider.example.test/api/v1/publish"

    def __init__(self, clock: Clock, *, stale_reads_per_commit: int = 0, ambiguous_update_finalize: bool = False) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._stale_reads_per_commit = stale_reads_per_commit
        self._ambiguous_update_finalize = ambiguous_update_finalize
        self._counter = 0
        self._versions: dict[str, ProviderVersion] = {}
        self._claims: dict[str, str] = {}
        self.live_html: bytes | None = None
        self._stale_html: bytes | None = None
        self._stale_reads_remaining = 0
        self.create_attempts = 0
        self.update_attempts = 0
        self.finalized_versions: list[str] = []
        self.marker_reads = 0
        self.image_checks = 0
        self.rate_limit_updates = 0
        self.bad_images = False
        self.crash_after: str | None = None

    def request_json(self, url: str, method: str, body: Mapping[str, Any] | None = None, *, headers: Mapping[str, Any] | None = None, timeout: float = 30) -> tuple[int, dict[str, Any]]:
        del headers, timeout
        with self._lock:
            if url == self.endpoint and method == "POST":
                self.create_attempts += 1
                slug = f"demo-{self.create_attempts}"
                claim = f"claim-{slug}-internal-only"
                version = self._new_version(slug)
                self._claims[slug] = claim
                return 201, {
                    "anonymous": True,
                    "slug": slug,
                    "siteUrl": f"https://{slug}.here.now/",
                    "expiresAt": self.expires_at,
                    "claimToken": claim,
                    "upload": self._upload_metadata(version),
                }
            if url == f"{self.endpoint}/demo-1" and method == "PUT":
                self.update_attempts += 1
                if self.rate_limit_updates:
                    self.rate_limit_updates -= 1
                    return 429, {"error": "rate_limited"}
                if not isinstance(body, Mapping) or body.get("claimToken") != self._claims.get("demo-1"):
                    return 403, {"error": "invalid_claim"}
                version = self._new_version("demo-1")
                return 200, {"expiresAt": self.expires_at, "upload": self._upload_metadata(version)}
            version = next((item for item in self._versions.values() if item.finalize_url == url), None)
            if version is not None and method == "POST":
                if not isinstance(body, Mapping) or body.get("versionId") != version.version_id or version.html is None:
                    return 400, {"error": "invalid_finalize"}
                self._commit(version)
                if self.crash_after == "finalize":
                    self.crash_after = None
                    raise SimulatedProcessExit("worker stopped after finalize")
                if self._ambiguous_update_finalize and version.slug == "demo-1":
                    self._ambiguous_update_finalize = False
                    raise TimeoutError("finalize response was lost after provider commit")
                return 200, {"publishStatus": {"expiresAt": self.expires_at}}
            raise AssertionError("fake HereNow received an unexpected JSON request")

    def upload(self, url: str, html: bytes, headers: Mapping[str, Any] | None = None, *, timeout: float = 30) -> None:
        del headers, timeout
        with self._lock:
            version = next((item for item in self._versions.values() if item.upload_url == url), None)
            if version is None:
                raise AssertionError("fake HereNow received an unknown signed upload URL")
            if len(html) > 1_048_576:
                raise AssertionError("remote publisher exceeded its verified HTML budget")
            version.html = bytes(html)
            if self.crash_after == "upload":
                self.crash_after = None
                raise SimulatedProcessExit("worker stopped after upload")

    def verify_markers(self, site_url: str, expected: Mapping[str, tuple[str, str]]) -> None:
        with self._lock:
            self.marker_reads += 1
            page = self._stale_html if self._stale_reads_remaining and self._stale_html is not None else self.live_html
            if self._stale_reads_remaining:
                self._stale_reads_remaining -= 1
            if page is None or not site_url.startswith("https://demo-"):
                raise HereNowError("page is not yet visible")
            for name, value in expected.values():
                marker = f'<meta name="{name}" content="{value}">'.encode("utf-8")
                if marker not in page:
                    raise HereNowError("page has stale markers")

    def verify_images(self, seed: Mapping[str, Any]) -> Mapping[str, Any]:
        del seed
        self.image_checks += 1
        if self.bad_images:
            raise HereNowError("current-round image policy failed")
        return {"scope": "currentRound", "images": 0, "uniqueImages": 0, "verified": []}

    @property
    def expires_at(self) -> str:
        # HereNow's reported timestamp must use the exact fixed-width value
        # embedded by the portable rolling compiler.
        return (self._clock.now() + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def current_page(self) -> bytes:
        if self.live_html is None:
            raise AssertionError("fake HereNow has no finalized page")
        return self.live_html

    def _new_version(self, slug: str) -> ProviderVersion:
        self._counter += 1
        version_id = f"version-{self._counter}"
        version = ProviderVersion(
            slug=slug,
            version_id=version_id,
            upload_url=f"https://uploads.example.test/{version_id}",
            finalize_url=f"https://finalize.example.test/{version_id}",
        )
        self._versions[version_id] = version
        return version

    @staticmethod
    def _upload_metadata(version: ProviderVersion) -> dict[str, Any]:
        return {
            "versionId": version.version_id,
            "uploads": [{"path": "index.html", "url": version.upload_url, "headers": {}}],
            "finalizeUrl": version.finalize_url,
        }

    def _commit(self, version: ProviderVersion) -> None:
        self._stale_html = self.live_html
        self.live_html = version.html
        self.finalized_versions.append(version.version_id)
        self._stale_reads_remaining = self._stale_reads_per_commit


class Environment:
    """One restartable local deployment sharing only durable test state."""

    def __init__(self, *, daily_quota_limit: int = 10, stale_reads_per_commit: int = 0, ambiguous_update_finalize: bool = False) -> None:
        self.clock = Clock()
        self.redis = RedisScriptHarness(self.clock)
        self.repository = RedisRepository(self.redis, now=self.clock.timestamp, prefix="winnow:test")
        self.security = CapabilitySecurity(capability_hmac_key=b"h" * 32, active_key_id="current", aead_keys={"current": b"c" * 32})
        self.fake_herenow = FakeHereNow(self.clock, stale_reads_per_commit=stale_reads_per_commit, ambiguous_update_finalize=ambiguous_update_finalize)
        self.config = CoordinatorConfig(
            max_wait_seconds=3,
            renewal_grace_seconds=2,
            creation_handoff_seconds=20,
            research_deadline_seconds=30,
            creating_ttl_seconds=30,
            daily_quota_limit=daily_quota_limit,
            quota_hmac_key=b"q" * 32,
        )
        self.notifier = InProcessWaitNotifier()
        self.restart()

    def publisher_factory(self, html_builder):
        return HereNowPublisher(
            endpoint=self.fake_herenow.endpoint,
            request_json=self.fake_herenow.request_json,
            upload=self.fake_herenow.upload,
            marker_verifier=self.fake_herenow.verify_markers,
            image_verifier=self.fake_herenow.verify_images,
            html_builder=html_builder,
            retry_delays_seconds=(0.0, 0.0, 0.0),
            retry_delay=lambda _seconds: None,
        )

    def restart(self) -> None:
        """Replace all process-local adapters while retaining Redis/provider state."""

        self.coordinator = Coordinator(self.repository, self.security, config=self.config, now=self.clock.now)
        self.rate_limiter = RateLimiter(self.repository, hmac_key=b"r" * 32, now=self.clock.timestamp)
        tool_config = McpToolConfig(coordinator_origin="https://coordinator.example/", max_wait_seconds=3, wait_poll_seconds=0.01)
        self.service = McpToolService(
            self.coordinator,
            publisher_factory=self.publisher_factory,
            rate_limiter=self.rate_limiter,
            notifier=self.notifier,
            config=tool_config,
        )
        self.app = create_app(
            AppConfig(coordinator_origin="https://coordinator.example/", mcp_allowed_hosts=("testserver",), mcp_max_wait_seconds=3, mcp_wait_poll_seconds=0.01),
            AppDependencies(
                coordinator=self.coordinator,
                publisher_factory=self.publisher_factory,
                rate_limiter=self.rate_limiter,
                notifier=self.notifier,
            ),
        )


class AsgiHarness:
    """Small in-memory HTTPS ASGI client; it performs no network I/O."""

    @staticmethod
    async def request(app, *, method: str, path: str, body: bytes = b"", headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, str], dict[str, Any] | None]:
        pairs = [(b"host", b"testserver")]
        for key, value in (headers or {}).items():
            pairs.append((key.lower().encode("latin-1"), value.encode("latin-1")))
        route, _, query = path.partition("?")
        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "https",
            "path": route,
            "raw_path": route.encode("ascii"),
            "query_string": query.encode("ascii"),
            "headers": pairs,
            "client": ("203.0.113.7", 443),
            "server": ("testserver", 443),
        }
        incoming = [{"type": "http.request", "body": body, "more_body": False}]
        sent: list[dict[str, Any]] = []

        async def receive():
            return incoming.pop(0) if incoming else {"type": "http.disconnect"}

        async def send(message):
            sent.append(dict(message))

        await app(scope, receive, send)
        start = next(item for item in sent if item["type"] == "http.response.start")
        raw = b"".join(item.get("body", b"") for item in sent if item["type"] == "http.response.body")
        response_headers = {key.decode("latin-1").lower(): value.decode("latin-1") for key, value in start["headers"]}
        return int(start["status"]), response_headers, json.loads(raw) if raw else None

    @classmethod
    async def mcp_result(cls, app, name: str, arguments: Mapping[str, Any]) -> Mapping[str, Any]:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": name, "arguments": dict(arguments)}},
            separators=(",", ":"),
        ).encode("utf-8")
        status, _headers, response = await cls.request(app, method="POST", path="/mcp", body=body, headers={"content-type": "application/json", "accept": "application/json"})
        if status != 200 or response is None:
            raise AssertionError(f"MCP tool did not return success: {status}")
        result = response.get("result")
        if not isinstance(result, Mapping):
            raise AssertionError("MCP response lacks a result")
        return result

    @classmethod
    async def mcp_tool(cls, app, name: str, arguments: Mapping[str, Any]) -> dict[str, Any]:
        result = await cls.mcp_result(app, name, arguments)
        if not isinstance(result.get("structuredContent"), Mapping):
            raise AssertionError("MCP response lacks structured content")
        return dict(result["structuredContent"])


def direct_create(environment: Environment, seed: Mapping[str, Any]) -> dict[str, Any]:
    token = _CURRENT_MCP_PROVENANCE.set(RequestProvenance(network_prefix="203.0.113.0/24", client_family="anthropic"))
    try:
        return asyncio.run(environment.service.create({"seed": dict(seed), "mode": "rolling"}))
    finally:
        _CURRENT_MCP_PROVENANCE.reset(token)


def wait_request(receipt: Mapping[str, Any]) -> WaitForContinueRequest:
    return WaitForContinueRequest.parse(
        {
            "sessionHandle": receipt["sessionHandle"],
            "expectedRoundNumber": receipt["roundNumber"],
            "expectedSeedHash": receipt["seedHash"],
            "maxWaitSeconds": 3,
        },
        maximum_wait_seconds=3,
    )


def browser_capability(environment: Environment, receipt: Mapping[str, Any]) -> str:
    stored = environment.repository.lookup_agent(environment.security.capability_hash(str(receipt["sessionHandle"])))
    if not isinstance(stored, ActiveSession):
        raise AssertionError("creation did not persist an active session")
    return environment.security.browser_capability_for_session(stored.session_id)


def browser_payload(seed: Mapping[str, Any], receipt: Mapping[str, Any], *, selected_profiles: list[str] | None = None, decisions: list[str] | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
    default = ["like", "dislike", "like", "dislike", "like", "skip"]
    decisions = decisions or default[: len(seed["round"]["options"])]
    return {
        "protocol": "winnow.browser-request",
        "version": 1,
        "idempotencyKey": idempotency_key or str(uuid.uuid4()),
        "roundNumber": receipt["roundNumber"],
        "seedHash": receipt["seedHash"],
        "publishedRevision": receipt["publishedRevision"],
        "verdicts": [{"optionId": option["id"], "decision": decision} for option, decision in zip(seed["round"]["options"], decisions)],
        "selectedProfileKeys": list(selected_profiles or []),
    }


def accepted_event(environment: Environment, seed: Mapping[str, Any], receipt: Mapping[str, Any], *, selected_profiles: list[str]) -> dict[str, Any]:
    first = environment.coordinator.wait_for_continue(str(receipt["sessionHandle"]), wait_request(receipt))
    if first["status"] != "still_waiting":
        raise AssertionError("agent wait did not register")
    browser = browser_capability(environment, receipt)
    request = BrowserNextRoundRequest.parse(browser_payload(seed, receipt, selected_profiles=selected_profiles))
    environment.coordinator.accept_browser_next_round(browser, origin="https://demo-1.here.now", request=request)
    event = environment.coordinator.wait_for_continue(str(receipt["sessionHandle"]), wait_request(receipt))
    if event["status"] != "continue_requested":
        raise AssertionError("accepted browser request did not release the agent")
    return event


def publish_arguments(receipt: Mapping[str, Any], event: Mapping[str, Any], successor: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "sessionHandle": receipt["sessionHandle"],
        "eventId": event["eventId"],
        "publishFence": event["publishFence"],
        "parentSeedHash": receipt["seedHash"],
        "nextSeed": dict(successor),
    }


def rolling_envelope(html: bytes) -> dict[str, Any]:
    matched = re.search(rb'<script id="winnow-rolling-page" type="application/octet-stream">([^<]+)</script>', html)
    if matched is None:
        raise AssertionError("rolling page envelope is missing")
    return json.loads(base64.b64decode(matched.group(1)).decode("utf-8"))


def hundred_option_seed() -> dict[str, Any]:
    """Return a schema-valid ten-round seed at the v1 option ceiling."""

    source_seed = fixture("synthetic-successor-seed.json")
    template = source_seed["history"][0]

    def round_value(number: int) -> dict[str, Any]:
        value = copy.deepcopy(template)
        value["number"] = number
        value["generatedAt"] = f"2026-08-{number:02d}T12:00:00Z"
        while len(value["sources"]) < 10:
            value["sources"].append(copy.deepcopy(value["sources"][len(value["sources"]) % 6]))
        while len(value["options"]) < 10:
            value["options"].append(copy.deepcopy(value["options"][len(value["options"]) % 6]))
        replacement_sources: dict[str, list[str]] = {}
        for index, source in enumerate(value["sources"], start=1):
            old = source["id"]
            replacement = f"capacity-source-{number}-{index}"
            replacement_sources.setdefault(old, []).append(replacement)
            source.update({"id": replacement, "title": f"Capacity source {number}-{index}", "url": f"https://example.com/capacity/{number}/{index}", "retrievedAt": value["generatedAt"]})
        for index, option in enumerate(value["options"], start=1):
            candidates = replacement_sources.get(option["primarySourceId"], [])
            source_id = candidates.pop(0) if candidates else value["sources"][index - 1]["id"]
            option.update({"id": f"capacity-option-{number}-{index}", "title": f"Capacity sofa {number}-{index}", "primarySourceId": source_id})
            option["description"].update({"text": f"Capacity description {number}-{index}.", "sourceId": source_id})
            option["image"].update({"url": f"https://example.com/capacity/{number}/{index}.png", "alt": option["title"], "sourceId": source_id})
            option["optionUrl"].update({"url": f"https://example.com/capacity/{number}/{index}", "sourceId": source_id})
            for factor_value in option["values"]:
                factor_value["sourceId"] = source_id
        value["verdicts"] = [{"optionId": option["id"], "decision": "skip"} for option in value["options"]]
        return value

    source_seed["history"] = [round_value(number) for number in range(1, 10)]
    source_seed["round"] = round_value(10)
    source_seed["round"].pop("verdicts")
    source_seed["profilePatterns"] = []
    _load_portable_core().validate_seed(source_seed)
    return source_seed


class RemoteIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.initial = fixture("synthetic-seed.json")
        self.successor = fixture("synthetic-successor-seed.json")
        self.selected_profile = fixture("synthetic-continuation.json")["profilePatterns"][0]["key"]

    def test_asgi_two_round_flow_converges_through_stale_reads_and_ambiguous_finalize(self) -> None:
        environment = Environment(stale_reads_per_commit=1, ambiguous_update_finalize=True)

        async def scenario() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
            async with environment.app.router.lifespan_context(environment.app):
                create_result = await AsgiHarness.mcp_result(environment.app, "create_winnow_session", {"seed": self.initial, "mode": "rolling"})
                receipt = dict(create_result["structuredContent"])
                self.assertEqual(receipt["status"], "awaiting_agent_wait")
                self.assertNotIn("claim", json.dumps(receipt).lower())
                browser = browser_capability(environment, receipt)
                wait_task = asyncio.create_task(
                    AsgiHarness.mcp_result(
                        environment.app,
                        "wait_for_continue",
                        {"sessionHandle": receipt["sessionHandle"], "expectedRoundNumber": 1, "expectedSeedHash": receipt["seedHash"], "maxWaitSeconds": 3},
                    )
                )
                for _attempt in range(50):
                    stored = environment.repository.lookup_agent(environment.security.capability_hash(receipt["sessionHandle"]))
                    if isinstance(stored, ActiveSession) and stored.phase == "accepting_request":
                        break
                    await asyncio.sleep(0)
                else:
                    self.fail("the MCP wait did not enter its durable accepting state")
                status_path = f"/v1/session/status?roundNumber=1&seedHash={receipt['seedHash']}&publishedRevision=1"
                status_code, _headers, status = await AsgiHarness.request(
                    environment.app,
                    method="GET",
                    path=status_path,
                    headers={"origin": "https://demo-1.here.now", "authorization": f"Bearer {browser}"},
                )
                self.assertEqual((status_code, status["status"]), (200, "connected"))
                payload = browser_payload(self.initial, receipt, selected_profiles=[self.selected_profile])
                accepted_code, _headers, accepted = await AsgiHarness.request(
                    environment.app,
                    method="POST",
                    path="/v1/session/next-round",
                    body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                    headers={"origin": "https://demo-1.here.now", "authorization": f"Bearer {browser}", "content-type": "application/json"},
                )
                self.assertEqual((accepted_code, accepted["status"]), (200, "accepted"))
                wait_result = await asyncio.wait_for(wait_task, timeout=1)
                event = dict(wait_result["structuredContent"])
                self.assertEqual(event["status"], "continue_requested")
                self.assertEqual(wait_result["content"][0]["annotations"], {"audience": ["assistant"]})
                self.assertEqual(
                    json.loads(wait_result["content"][0]["text"]),
                    {
                        "nextAction": "Research a valid successor, then call publish_next_round with publishArguments and nextSeed.",
                        "publishArguments": {
                            "sessionHandle": receipt["sessionHandle"],
                            "eventId": event["eventId"],
                            "publishFence": event["publishFence"],
                            "parentSeedHash": receipt["seedHash"],
                        },
                        "continuation": event["continuation"],
                        "researchDeadline": event["researchDeadline"],
                    },
                )
                publish_result = await AsgiHarness.mcp_result(environment.app, "publish_next_round", publish_arguments(receipt, event, self.successor))
                published = dict(publish_result["structuredContent"])
                self.assertEqual((published["status"], published["roundNumber"], published["publishedRevision"]), ("awaiting_agent_wait", 2, 2))
                self.assertEqual(publish_result["content"][0]["annotations"], {"audience": ["assistant"]})
                self.assertEqual(
                    json.loads(publish_result["content"][0]["text"]),
                    {
                        "nextTool": "wait_for_continue",
                        "arguments": {
                            "sessionHandle": receipt["sessionHandle"],
                            "expectedRoundNumber": published["roundNumber"],
                            "expectedSeedHash": published["seedHash"],
                            "maxWaitSeconds": environment.service.max_wait_seconds,
                        },
                    },
                )
                ready_code, _headers, ready = await AsgiHarness.request(
                    environment.app,
                    method="GET",
                    path=status_path,
                    headers={"origin": "https://demo-1.here.now", "authorization": f"Bearer {browser}"},
                )
                self.assertEqual((ready_code, ready["status"]), (200, "ready_to_reveal"))
                # This is the user selecting Continue: it reloads the stable
                # HereNow URL and receives the committed revision, not an API
                # command or agent/chat interaction.
                continued = rolling_envelope(environment.fake_herenow.current_page())
                self.assertEqual(continued["publishedRevision"], 2)
                second_wait = asyncio.create_task(
                    AsgiHarness.mcp_tool(
                        environment.app,
                        "wait_for_continue",
                        {"sessionHandle": receipt["sessionHandle"], "expectedRoundNumber": 2, "expectedSeedHash": published["seedHash"], "maxWaitSeconds": 3},
                    )
                )
                for _attempt in range(50):
                    stored = environment.repository.lookup_agent(environment.security.capability_hash(receipt["sessionHandle"]))
                    if isinstance(stored, ActiveSession) and stored.current_round_number == 2 and stored.phase == "accepting_request":
                        break
                    await asyncio.sleep(0)
                else:
                    self.fail("the same agent did not enter the second wait")
                second_payload = browser_payload(self.successor, published)
                second_code, _headers, second_accepted = await AsgiHarness.request(
                    environment.app,
                    method="POST",
                    path="/v1/session/next-round",
                    body=json.dumps(second_payload, separators=(",", ":")).encode("utf-8"),
                    headers={"origin": "https://demo-1.here.now", "authorization": f"Bearer {browser}", "content-type": "application/json"},
                )
                self.assertEqual((second_code, second_accepted["status"]), (200, "accepted"))
                self.assertEqual((await asyncio.wait_for(second_wait, timeout=1))["status"], "continue_requested")
                return receipt, event, published, ready

        receipt, event, published, ready = asyncio.run(scenario())
        self.assertEqual((receipt["roundNumber"], published["roundNumber"], ready["publishedRevision"]), (1, 2, 2))
        self.assertEqual(receipt["expiresAt"], published["expiresAt"])
        self.assertEqual(len(environment.fake_herenow.finalized_versions), 2)
        self.assertEqual(environment.fake_herenow.update_attempts, 1)
        self.assertGreaterEqual(environment.fake_herenow.marker_reads, 3)
        self.assertIn(RedisRepository._CREATE, environment.redis.evaluated_scripts)
        self.assertIn(RedisRepository._CAS, environment.redis.evaluated_scripts)
        self.assertNotIn(browser_capability(environment, receipt), json.dumps(event))

    def test_restart_recovery_preserves_event_and_converges_after_upload_finalize_commit_and_tombstone(self) -> None:
        def create_accepted_request(environment: Environment) -> dict[str, Any]:
            receipt = direct_create(environment, self.initial)
            self.assertEqual(receipt["status"], "awaiting_agent_wait")
            first = environment.coordinator.wait_for_continue(receipt["sessionHandle"], wait_request(receipt))
            self.assertEqual(first["status"], "still_waiting")
            environment.coordinator.accept_browser_next_round(
                browser_capability(environment, receipt),
                origin="https://demo-1.here.now",
                request=BrowserNextRoundRequest.parse(browser_payload(self.initial, receipt, selected_profiles=[self.selected_profile])),
            )
            return receipt

        upload_environment = Environment()
        receipt = create_accepted_request(upload_environment)
        upload_environment.restart()  # restart after browser acceptance, before first delivery
        event = upload_environment.coordinator.wait_for_continue(receipt["sessionHandle"], wait_request(receipt))
        self.assertEqual(event["status"], "continue_requested")
        upload_environment.restart()  # restart after the first delivery response
        redelivered = upload_environment.coordinator.wait_for_continue(receipt["sessionHandle"], wait_request(receipt))
        self.assertEqual((redelivered["eventId"], redelivered["publishFence"]), (event["eventId"], event["publishFence"]))
        upload_environment.fake_herenow.crash_after = "upload"
        arguments = publish_arguments(receipt, event, self.successor)
        with self.assertRaises(SimulatedProcessExit):
            asyncio.run(upload_environment.service.publish(arguments))
        upload_environment.restart()  # no process-local coordinator/provider state is reused
        recovered_upload = asyncio.run(upload_environment.service.publish(arguments))
        self.assertEqual((recovered_upload["status"], recovered_upload["publishedRevision"]), ("awaiting_agent_wait", 2))
        self.assertEqual((upload_environment.fake_herenow.update_attempts, len(upload_environment.fake_herenow.finalized_versions)), (2, 2))
        upload_environment.restart()  # restart after commit
        browser = browser_capability(upload_environment, receipt)
        self.assertEqual(
            upload_environment.coordinator.browser_status(browser, origin="https://demo-1.here.now", embedded_revision=1)["status"],
            "ready_to_reveal",
        )
        upload_environment.coordinator.fail_session(receipt["sessionHandle"])
        upload_environment.restart()  # restart after terminal sanitization
        self.assertEqual(
            upload_environment.coordinator.browser_status(browser, origin="https://demo-1.here.now", embedded_revision=2)["status"],
            "failed",
        )

        finalize_environment = Environment()
        final_receipt = create_accepted_request(finalize_environment)
        final_event = finalize_environment.coordinator.wait_for_continue(final_receipt["sessionHandle"], wait_request(final_receipt))
        final_arguments = publish_arguments(final_receipt, final_event, self.successor)
        finalize_environment.fake_herenow.crash_after = "finalize"
        with self.assertRaises(SimulatedProcessExit):
            asyncio.run(finalize_environment.service.publish(final_arguments))
        finalize_environment.restart()
        recovered_finalize = asyncio.run(finalize_environment.service.publish(final_arguments))
        self.assertEqual((recovered_finalize["status"], recovered_finalize["publishedRevision"]), ("awaiting_agent_wait", 2))
        self.assertEqual((finalize_environment.fake_herenow.update_attempts, len(finalize_environment.fake_herenow.finalized_versions)), (1, 2))

    def test_capacity_payload_lineage_images_circuit_quotas_rate_limits_and_ttl(self) -> None:
        # A browser request larger than the strict endpoint limit is rejected
        # before JSON parsing and leaves the agent wait viable.
        payload_environment = Environment()
        payload_receipt = direct_create(payload_environment, self.initial)
        payload_environment.coordinator.wait_for_continue(payload_receipt["sessionHandle"], wait_request(payload_receipt))
        browser = browser_capability(payload_environment, payload_receipt)

        async def oversized_browser_request() -> tuple[int, dict[str, Any] | None]:
            status, _headers, body = await AsgiHarness.request(
                payload_environment.app,
                method="POST",
                path="/v1/session/next-round",
                body=b"{" + b"x" * MAX_REMOTE_BROWSER_REQUEST_BYTES,
                headers={"origin": "https://demo-1.here.now", "authorization": f"Bearer {browser}", "content-type": "application/json"},
            )
            return status, body

        self.assertEqual(asyncio.run(oversized_browser_request()), (400, {"error": "request_rejected"}))
        self.assertEqual(
            payload_environment.coordinator.browser_status(browser, origin="https://demo-1.here.now", embedded_revision=1)["status"],
            "connected",
        )

        # Duplicate lineage cannot spend the durable event or trigger a provider update.
        lineage_environment = Environment()
        lineage_receipt = direct_create(lineage_environment, self.initial)
        lineage_event = accepted_event(lineage_environment, self.initial, lineage_receipt, selected_profiles=[self.selected_profile])
        duplicate = copy.deepcopy(self.successor)
        duplicate["round"]["options"][0]["id"] = self.initial["round"]["options"][0]["id"]
        rejected = asyncio.run(lineage_environment.service.publish(publish_arguments(lineage_receipt, lineage_event, duplicate)))
        self.assertEqual(rejected["status"], "rejected")
        self.assertEqual(lineage_environment.fake_herenow.update_attempts, 0)

        # Hosted image failures stop creation before HereNow is contacted.
        image_environment = Environment()
        image_environment.fake_herenow.bad_images = True
        self.assertEqual(direct_create(image_environment, self.initial)["status"], "rejected")
        self.assertEqual(image_environment.fake_herenow.create_attempts, 0)

        # The v1 option ceiling terminalizes a live waiter instead of accepting
        # an impossible successor.  This record is schema-valid and is stored
        # through the production Redis create/CAS path.
        capacity_environment = Environment()
        capacity_seed = hundred_option_seed()
        capacity_browser = capacity_environment.security.browser_capability_for_session("capacity-session")
        capacity_agent = capacity_environment.security.new_capability()
        now = capacity_environment.clock.timestamp()
        capacity_environment.repository.create(
            ActiveSession(
                record_version=0,
                session_id="capacity-session",
                browser_capability_hash=capacity_environment.security.capability_hash(capacity_browser),
                agent_capability_hash=capacity_environment.security.capability_hash(capacity_agent),
                seed=capacity_seed,
                seed_hash=_load_portable_core().seed_hash(capacity_seed),
                current_round_number=10,
                published_revision=10,
                phase="accepting_request",
                created_at=now,
                original_expires_at="2026-08-18T13:00:00Z",
                expires_at=now + 3600,
                site_url="https://capacity.here.now/",
                slug="capacity",
                allowed_origin="https://capacity.here.now",
                agent_state="waiting",
                wait_deadline=now + 10,
            )
        )
        capacity_request = BrowserNextRoundRequest.parse(
            browser_payload(
                capacity_seed,
                {"roundNumber": 10, "seedHash": _load_portable_core().seed_hash(capacity_seed), "publishedRevision": 10},
                decisions=["skip"] * 10,
            )
        )
        capacity = capacity_environment.coordinator.accept_browser_next_round(capacity_browser, origin="https://capacity.here.now", request=capacity_request)
        self.assertEqual(capacity["status"], "complete")

        circuit_environment = Environment()
        circuit_receipt = direct_create(circuit_environment, self.initial)
        circuit_environment.coordinator.wait_for_continue(circuit_receipt["sessionHandle"], wait_request(circuit_receipt))
        circuit_environment.coordinator.set_circuit_mode("read_only_existing")
        circuit = circuit_environment.coordinator.accept_browser_next_round(
            browser_capability(circuit_environment, circuit_receipt),
            origin="https://demo-1.here.now",
            request=BrowserNextRoundRequest.parse(browser_payload(self.initial, circuit_receipt)),
        )
        self.assertEqual(circuit["status"], "circuit_open")

        quota_environment = Environment(daily_quota_limit=1)
        self.assertEqual(direct_create(quota_environment, self.initial)["status"], "awaiting_agent_wait")
        self.assertEqual(direct_create(quota_environment, self.initial)["status"], "rejected")
        rate_environment = Environment()
        rate_receipt = direct_create(rate_environment, self.initial)
        rate_event = accepted_event(rate_environment, self.initial, rate_receipt, selected_profiles=[self.selected_profile])
        rate_browser = browser_capability(rate_environment, rate_receipt)
        rate_environment.fake_herenow.rate_limit_updates = 1
        self.assertEqual(asyncio.run(rate_environment.service.publish(publish_arguments(rate_receipt, rate_event, self.successor)))["status"], "rejected")
        self.assertEqual(
            rate_environment.coordinator.browser_status(rate_browser, origin="https://demo-1.here.now", embedded_revision=1)["status"],
            "failed",
        )
        ttl_environment = Environment()
        ttl_receipt = direct_create(ttl_environment, self.initial)
        ttl_environment.clock.advance(3601)
        self.assertIsNone(ttl_environment.repository.lookup_agent(ttl_environment.security.capability_hash(ttl_receipt["sessionHandle"])))
        self.assertIn(RedisRepository._QUOTA, ttl_environment.redis.evaluated_scripts)

    def test_concurrent_browsers_and_agents_converge_on_one_fenced_event(self) -> None:
        environment = Environment()
        receipt = direct_create(environment, self.initial)
        environment.coordinator.wait_for_continue(receipt["sessionHandle"], wait_request(receipt))
        browser = browser_capability(environment, receipt)
        requests = [
            BrowserNextRoundRequest.parse(browser_payload(self.initial, receipt, selected_profiles=[self.selected_profile]))
            for _index in range(2)
        ]
        barrier = threading.Barrier(2)

        def submit(request: BrowserNextRoundRequest) -> str:
            barrier.wait()
            try:
                return environment.coordinator.accept_browser_next_round(browser, origin="https://demo-1.here.now", request=request)["status"]
            except StateConflict:
                return "conflict"

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(submit, requests))
        self.assertEqual(sorted(outcomes), ["accepted", "conflict"])

        def receive_event() -> tuple[str, str]:
            event = environment.coordinator.wait_for_continue(receipt["sessionHandle"], wait_request(receipt))
            return event["eventId"], event["publishFence"]

        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            delivered = list(executor.map(lambda _item: receive_event(), range(2)))
        self.assertEqual(len(set(delivered)), 1)
        self.assertIn(RedisRepository._CAS, environment.redis.evaluated_scripts)


if __name__ == "__main__":
    unittest.main()
