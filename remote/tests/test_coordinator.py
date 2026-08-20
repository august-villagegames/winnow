from __future__ import annotations

import copy
import json
import subprocess
import sys
import threading
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "remote" / "src"))

from winnow_remote.contracts import (  # noqa: E402
    BrowserNextRoundRequest,
    ContractError,
    PublishNextRoundRequest,
    WaitForContinueRequest,
)
from winnow_remote.coordinator import (  # noqa: E402
    AuthenticationError,
    CircuitOpen,
    CoordinatorError,
    Coordinator,
    CoordinatorConfig,
    QuotaExceeded,
    StateConflict,
    canonical_preflight_origin,
    normalize_network_prefix,
    reconstruct_continuation,
)
from winnow_remote.herenow import _load_portable_core  # noqa: E402
from winnow_remote.repository import ActiveSession, FakeRepository, TerminalTombstone  # noqa: E402
from winnow_remote.security import CapabilitySecurity, EncryptedSecret, SecretError  # noqa: E402


class Clock:
    def __init__(self):
        self.value = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)

    def now(self):
        return self.value

    def timestamp(self):
        return self.value.timestamp()

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


def fixture(name="synthetic-seed.json"):
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.security = CapabilitySecurity(
            capability_hmac_key=b"h" * 32,
            active_key_id="current",
            aead_keys={"old": b"o" * 32, "current": b"c" * 32},
        )
        self.repository = FakeRepository(now=self.clock.timestamp)
        self.config = CoordinatorConfig(
            max_wait_seconds=10,
            renewal_grace_seconds=3,
            creation_handoff_seconds=20,
            research_deadline_seconds=30,
            creating_ttl_seconds=60,
            quota_hmac_key=b"q" * 32,
        )
        self.coordinator = Coordinator(self.repository, self.security, config=self.config, now=self.clock.now)
        self.seed = fixture()

    def active(self):
        handle = self.coordinator.begin_creation(self.seed, network_prefix="203.0.113.0/24", client_family="anthropic")
        self.coordinator.persist_creation_publication(
            handle,
            site_url="https://demo.here.now/",
            slug="demo",
            original_expires_at="2026-08-19T12:00:00Z",
            claim_token="provider-claim-token",
        )
        receipt = self.coordinator.activate_creation(handle)
        self.assertEqual(receipt["sessionHandle"], handle.agent_capability)
        self.assertNotIn(handle.browser_capability, json.dumps(receipt))
        self.assertNotIn("provider-claim-token", json.dumps(receipt))
        return handle, receipt

    def wait_request(self, seed_hash=None):
        return WaitForContinueRequest(
            expected_round_number=1,
            expected_seed_hash=seed_hash or self._core().seed_hash(self.seed),
            max_wait_seconds=10,
        )

    @staticmethod
    def _core():
        return _load_portable_core()

    def browser_request(self, *, selected=None, idempotency=None, seed_hash=None, decisions=None):
        decisions = decisions or ["like", "dislike", "like", "dislike", "like", "skip"]
        verdicts = [{"optionId": option["id"], "decision": decision} for option, decision in zip(self.seed["round"]["options"], decisions)]
        continuation = fixture("synthetic-continuation.json")
        return BrowserNextRoundRequest.parse(
            {
                "protocol": "winnow.browser-request",
                "version": 1,
                "idempotencyKey": idempotency or str(uuid.uuid4()),
                "roundNumber": 1,
                "seedHash": seed_hash or self._core().seed_hash(self.seed),
                "publishedRevision": 1,
                "verdicts": verdicts,
                "selectedProfileKeys": selected if selected is not None else [continuation["profilePatterns"][0]["key"]],
            }
        )

    def publish_request(self, event, *, seed=None):
        return PublishNextRoundRequest.parse(
            {
                "sessionHandle": "placeholder",
                "eventId": event["eventId"],
                "publishFence": event["publishFence"],
                "parentSeedHash": self._core().seed_hash(self.seed),
                "nextSeed": seed or fixture("synthetic-successor-seed.json"),
            }
        )

    def accept(self, handle, request=None):
        request = request or self.browser_request()
        return self.coordinator.accept_browser_next_round(handle.browser_capability, origin="https://demo.here.now", request=request)

    def event(self, handle):
        self.coordinator.wait_for_continue(handle.agent_capability, self.wait_request())
        self.accept(handle)
        event = self.coordinator.wait_for_continue(handle.agent_capability, self.wait_request())
        self.assertEqual(event["status"], "continue_requested")
        return event

    def test_closed_browser_contract_rejects_legacy_continuations_free_form_and_noncanonical_verdicts(self):
        valid = self.browser_request()
        raw = {
            "protocol": "winnow.browser-request",
            "version": 1,
            "idempotencyKey": str(uuid.uuid4()),
            "roundNumber": 1,
            "seedHash": valid.seed_hash,
            "publishedRevision": 1,
            "verdicts": [item.as_dict() for item in valid.verdicts],
            "selectedProfileKeys": [],
        }
        for extra in (
            {"continuation": fixture("synthetic-continuation.json")},
            {"profileExclusions": ["free-form preference"]},
            {"profilePatterns": [{"text": "free form"}]},
        ):
            with self.subTest(extra=next(iter(extra))):
                with self.assertRaisesRegex(ContractError, "unknown properties"):
                    BrowserNextRoundRequest.parse({**raw, **extra})
        reordered = copy.deepcopy(raw)
        reordered["verdicts"][0], reordered["verdicts"][1] = reordered["verdicts"][1], reordered["verdicts"][0]
        handle, _receipt = self.active()
        self.coordinator.wait_for_continue(handle.agent_capability, self.wait_request())
        with self.assertRaisesRegex(ContractError, "canonical order"):
            self.accept(handle, BrowserNextRoundRequest.parse(reordered))
        invalid_key = copy.deepcopy(raw)
        invalid_key["selectedProfileKeys"] = ["not-derived"]
        with self.assertRaisesRegex(ContractError, "canonical-order subset"):
            self.accept(handle, BrowserNextRoundRequest.parse(invalid_key))

    def test_browser_accepts_canonical_category_profile_key_with_internal_spaces(self):
        """Category profile keys are canonical JSON, not whitespace-free tokens."""

        for option_index in (0, 2, 4):
            for value in self.seed["round"]["options"][option_index]["values"]:
                if value["factorId"] == "seats":
                    value["value"] = "three seat"

        category_key = json.dumps(
            {"direction": "include", "factorId": "seats", "polarity": "like", "value": "three seat"},
            separators=(",", ":"),
        )
        request = self.browser_request(selected=[category_key])
        handle, _receipt = self.active()
        self.coordinator.wait_for_continue(handle.agent_capability, self.wait_request())

        accepted = self.accept(handle, request)
        self.assertEqual(accepted["status"], "accepted")

        invalid_key = category_key.replace("three seat", "three\nseat")
        raw = {
            "protocol": "winnow.browser-request",
            "version": 1,
            "idempotencyKey": str(uuid.uuid4()),
            "roundNumber": 1,
            "seedHash": self._core().seed_hash(self.seed),
            "publishedRevision": 1,
            "verdicts": [item.as_dict() for item in request.verdicts],
            "selectedProfileKeys": [invalid_key],
        }
        with self.assertRaisesRegex(ContractError, "invalid characters"):
            BrowserNextRoundRequest.parse(raw)

    def test_reconstruction_matches_cross_language_runtime_fixture_and_keeps_parent_server_owned(self):
        continuation = fixture("synthetic-continuation.json")
        request = self.browser_request(
            seed_hash=continuation["parent"]["seedHash"],
            selected=[continuation["profilePatterns"][0]["key"]],
        )
        rebuilt = reconstruct_continuation(
            self.seed,
            seed_hash=continuation["parent"]["seedHash"],
            site_url=continuation["parent"]["url"],
            request=request,
            core=self._core(),
        )
        self.assertEqual(rebuilt, continuation)

    def test_server_reconstruction_matches_the_current_runtime_core_output(self):
        continuation = fixture("synthetic-continuation.json")
        request = self.browser_request(
            seed_hash=continuation["parent"]["seedHash"],
            selected=[continuation["profilePatterns"][0]["key"]],
        )
        rebuilt = reconstruct_continuation(
            self.seed,
            seed_hash=continuation["parent"]["seedHash"],
            site_url=continuation["parent"]["url"],
            request=request,
            core=self._core(),
        )
        node = r'''
const fs = require("node:fs");
const core = require("./.agents/skills/winnow/assets/runtime-core.js");
const seed = JSON.parse(fs.readFileSync("./fixtures/synthetic-seed.json", "utf8"));
const continuation = JSON.parse(fs.readFileSync("./fixtures/synthetic-continuation.json", "utf8"));
const decisions = Object.fromEntries(continuation.completedRounds[0].verdicts.map((item) => [item.optionId, item.decision]));
const result = core.buildContinuation(seed, decisions, continuation.parent.seedHash, continuation.parent.url, seed.profileExclusions, continuation.profilePatterns);
process.stdout.write(JSON.stringify(result));
'''
        completed = subprocess.run(["node", "-e", node], cwd=ROOT, text=True, capture_output=True, check=False)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        # The supplied selected profile record is intentionally server-derived;
        # Core's explicit profile selection therefore has the same bytes.
        from_runtime = json.loads(completed.stdout)
        self.assertEqual(rebuilt, from_runtime)

    def test_wait_epoch_renews_only_inside_grace_and_late_renewal_tombstones(self):
        handle, _receipt = self.active()
        first = self.coordinator.wait_for_continue(handle.agent_capability, self.wait_request())
        self.assertEqual((first["status"], first["waitEpoch"]), ("still_waiting", 1))
        self.clock.advance(11)
        renewed = self.coordinator.wait_for_continue(handle.agent_capability, self.wait_request())
        self.assertEqual((renewed["status"], renewed["waitEpoch"]), ("still_waiting", 1))
        self.clock.advance(14)
        terminal = self.coordinator.wait_for_continue(handle.agent_capability, self.wait_request())
        self.assertEqual(terminal["status"], "disconnected")

    def test_concurrent_browser_submissions_accept_exactly_one_event_and_same_digest_is_idempotent(self):
        handle, _receipt = self.active()
        self.coordinator.wait_for_continue(handle.agent_capability, self.wait_request())
        requests = [self.browser_request(), self.browser_request()]
        results, errors = [], []

        def submit(request):
            try:
                results.append(self.accept(handle, request))
            except StateConflict:
                errors.append("conflict")

        threads = [threading.Thread(target=submit, args=(request,)) for request in requests]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        accepted = requests[0] if not errors or results else requests[0]
        # One of the requests won; replay each until the accepted one is found.
        replayed = []
        for request in requests:
            try:
                replayed.append(self.accept(handle, request))
            except StateConflict:
                pass
        self.assertEqual(len(replayed), 1)
        self.assertEqual(replayed[0]["status"], "accepted")

    def test_idempotency_digest_mismatch_is_rejected_without_second_event(self):
        handle, _receipt = self.active()
        self.coordinator.wait_for_continue(handle.agent_capability, self.wait_request())
        request = self.browser_request()
        self.accept(handle, request)
        changed = self.browser_request(idempotency=request.idempotency_key, decisions=["skip"] * 6)
        with self.assertRaisesRegex(StateConflict, "different request"):
            self.accept(handle, changed)
        event = self.coordinator.wait_for_continue(handle.agent_capability, self.wait_request())
        self.assertEqual(event["status"], "continue_requested")
        self.assertEqual(event["continuation"]["completedRounds"][-1]["verdicts"][0]["decision"], "like")

    def test_event_redelivery_restart_and_pending_publication_survive_without_reissuing_event(self):
        handle, _receipt = self.active()
        event = self.event(handle)
        restarted = Coordinator(self.repository, self.security, config=self.config, now=self.clock.now)
        redelivered = restarted.wait_for_continue(handle.agent_capability, self.wait_request())
        self.assertEqual((redelivered["eventId"], redelivered["publishFence"]), (event["eventId"], event["publishFence"]))
        publish = self.publish_request(event)
        self.assertEqual(restarted.begin_publish(handle.agent_capability, publish)["status"], "publishing")
        restarted.persist_pending_publication(handle.agent_capability, publish_fence=event["publishFence"], pending_publication={"versionId": "v2", "revision": 2})
        recovered = Coordinator(self.repository, self.security, config=self.config, now=self.clock.now)
        committed = recovered.commit_publish(handle.agent_capability, publish)
        self.assertEqual((committed["roundNumber"], committed["publishedRevision"]), (2, 2))

    def test_publish_fence_and_revision_cas_prevent_stale_or_concurrent_commit(self):
        handle, _receipt = self.active()
        event = self.event(handle)
        request = self.publish_request(event)
        self.coordinator.begin_publish(handle.agent_capability, request)
        with self.assertRaises(StateConflict):
            self.coordinator.begin_publish(handle.agent_capability, request)
        committed = self.coordinator.commit_publish(handle.agent_capability, request)
        self.assertEqual(committed["publishedRevision"], 2)
        with self.assertRaises(StateConflict):
            self.coordinator.commit_publish(handle.agent_capability, request)

    def test_capabilities_cannot_cross_use_and_claim_ciphertext_is_bound_to_session_and_key(self):
        handle, _receipt = self.active()
        with self.assertRaises(AuthenticationError):
            self.coordinator.wait_for_continue(handle.browser_capability, self.wait_request())
        with self.assertRaises(AuthenticationError):
            self.coordinator.browser_status(handle.agent_capability, origin="https://demo.here.now", embedded_revision=1)
        encrypted = self.security.encrypt_claim_token(session_id="one", claim_token="claim")
        self.assertEqual(self.security.decrypt_claim_token(session_id="one", encrypted=encrypted), "claim")
        with self.assertRaises(SecretError):
            self.security.decrypt_claim_token(session_id="two", encrypted=encrypted)
        self.assertNotIn("claim", repr(encrypted))
        active = self.repository.get(handle.session_id)
        self.assertNotIn("provider-claim-token", json.dumps(active.as_dict()))

    def test_tombstone_sanitizes_content_keeps_hashes_and_enforces_exact_terminal_cors(self):
        handle, _receipt = self.active()
        terminal = self.coordinator.fail_session(handle.agent_capability)
        self.assertEqual(terminal["status"], "failed")
        tombstone = self.repository.lookup_browser(self.security.capability_hash(handle.browser_capability))
        self.assertIsInstance(tombstone, TerminalTombstone)
        stored = tombstone.as_dict()
        serialized = json.dumps(stored)
        for forbidden in ("seed", "continuation", "claim", "publish_fence", "pending_publication"):
            self.assertNotIn(forbidden, serialized)
        status = self.coordinator.browser_status(handle.browser_capability, origin="https://demo.here.now", embedded_revision=1)
        self.assertEqual((status["status"], status["corsOrigin"]), ("failed", "https://demo.here.now"))
        with self.assertRaises(AuthenticationError):
            self.coordinator.browser_status(handle.browser_capability, origin="https://evil.here.now", embedded_revision=1)

    def test_quota_circuit_and_canonical_preflight_policy(self):
        self.assertEqual(normalize_network_prefix("203.0.113.77"), "203.0.113.0/24")
        self.assertEqual(normalize_network_prefix("2001:db8:1234:5678::1"), "2001:db8:1234:5678::/64")
        self.assertTrue(canonical_preflight_origin("https://demo.here.now", allowed_host_suffixes=(".here.now",)))
        self.assertFalse(canonical_preflight_origin("https://demo.here.now/extra", allowed_host_suffixes=(".here.now",)))
        self.coordinator.set_circuit_mode("no_new_sessions")
        with self.assertRaises(CircuitOpen):
            self.coordinator.begin_creation(self.seed, network_prefix="203.0.113.0/24", client_family="anthropic")
        limited = Coordinator(
            FakeRepository(now=self.clock.timestamp),
            self.security,
            config=CoordinatorConfig(daily_quota_limit=1, quota_hmac_key=b"z" * 32),
            now=self.clock.now,
        )
        limited.begin_creation(self.seed, network_prefix="198.51.100.0/24", client_family="untrusted-label")
        with self.assertRaises(QuotaExceeded):
            limited.begin_creation(self.seed, network_prefix="198.51.100.0/24", client_family="another-untrusted-label")

    def test_illegal_phase_edges_are_rejected(self):
        handle, _receipt = self.active()
        eventless = self.browser_request()
        with self.assertRaises(StateConflict):
            self.accept(handle, eventless)
        with self.assertRaises(StateConflict):
            self.coordinator.begin_publish(handle.agent_capability, PublishNextRoundRequest.parse({
                "sessionHandle": handle.agent_capability,
                "eventId": "event",
                "publishFence": "fence",
                "parentSeedHash": self._core().seed_hash(self.seed),
                "nextSeed": fixture("synthetic-successor-seed.json"),
            }))

    def test_oversized_continuation_is_rejected_before_it_can_wake_an_agent(self):
        seed = fixture()
        source = seed["round"]["sources"][0]
        for index in range(220):
            extra = copy.deepcopy(source)
            extra["id"] = f"large-source-{index}"
            extra["title"] = "x" * 200
            extra["url"] = f"https://example.com/large-source-{index}"
            seed["round"]["sources"].append(extra)
        handle = self.coordinator.begin_creation(seed, network_prefix="203.0.113.0/24", client_family="anthropic")
        self.coordinator.persist_creation_publication(
            handle,
            site_url="https://large.here.now/",
            slug="large",
            original_expires_at="2026-08-19T12:00:00Z",
            claim_token="provider-claim-token",
        )
        self.coordinator.activate_creation(handle)
        self.coordinator.wait_for_continue(handle.agent_capability, WaitForContinueRequest(expected_round_number=1, expected_seed_hash=self._core().seed_hash(seed), max_wait_seconds=10))
        request = BrowserNextRoundRequest.parse({
            "protocol": "winnow.browser-request",
            "version": 1,
            "idempotencyKey": str(uuid.uuid4()),
            "roundNumber": 1,
            "seedHash": self._core().seed_hash(seed),
            "publishedRevision": 1,
            "verdicts": [{"optionId": option["id"], "decision": "skip"} for option in seed["round"]["options"]],
            "selectedProfileKeys": [],
        })
        with self.assertRaisesRegex(CoordinatorError, "MCP result exceeds"):
            self.coordinator.accept_browser_next_round(handle.browser_capability, origin="https://large.here.now", request=request)
        stored = self.repository.lookup_agent(self.security.capability_hash(handle.agent_capability))
        self.assertIsInstance(stored, ActiveSession)
        self.assertEqual((stored.phase, stored.agent_state, stored.accepted_event), ("accepting_request", "waiting", None))
        status = self.coordinator.browser_status(handle.browser_capability, origin="https://large.here.now", embedded_revision=1)
        self.assertEqual(status["status"], "connected")


if __name__ == "__main__":
    unittest.main()
