from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "remote" / "src"))

from winnow_remote.herenow import (  # noqa: E402
    HERE_NOW_CONTENT_TYPE,
    HereNowError,
    HereNowPublisher,
    MAX_REMOTE_COMPILED_HTML_BYTES,
    expected_live_markers,
)


EXPIRY = "2026-08-19T12:00:00.000Z"
CLAIM = "claim-token-must-remain-internal"


class Created:
    def __init__(self):
        self.site_url = "https://demo.here.now/"
        self.expires_at = EXPIRY
        self.slug = "demo"
        self.claim_token = CLAIM
        self.upload_url = "https://upload.example/signed"
        self.upload_headers = {"X-Upload": "signed"}
        self.finalize_url = "https://finalize.example/signed"
        self.version_id = "version-1"


class FakeCore:
    RUNTIME_VERSION = "4.0.0"

    def __init__(self, events, *, final_html_delta=0):
        self.events = events
        self.final_html_delta = final_html_delta

    @staticmethod
    def canonical_json(value):
        return json.dumps(value, sort_keys=True, separators=(",", ":")).encode()

    @staticmethod
    def seed_hash(_seed):
        return "a" * 64

    def validate_seed(self, _seed):
        self.events.append("validate")

    def build_html(self, _seed, *, expires_at=None):
        self.events.append("build-final" if expires_at else "build-provisional")
        marker = (expires_at or "0000-00-00T00:00:00.000Z").encode()
        return (b"x" * 100) + marker + (b"!" * (self.final_html_delta if expires_at else 0))

    def create_anonymous_site_internal(self, html, *, endpoint, require_claim_token, request_json, client_header):
        self.events.append("create")
        self.create_html_size = len(html)
        self.create_endpoint = endpoint
        self.require_claim_token = require_claim_token
        self.client_header = client_header
        return Created()


def seed():
    return {"session": {"id": "session-1"}, "round": {"number": 1}}


def markers(revision=1):
    return expected_live_markers(
        session_id="session-1",
        seed_hash="a" * 64,
        runtime_version="4.0.0",
        expires_at=EXPIRY,
        rolling_version=1,
        published_revision=revision,
    )


class HereNowPublisherTests(unittest.TestCase):
    def test_live_markers_normalize_an_equivalent_provider_expiration(self):
        value = expected_live_markers(
            session_id="session-1",
            seed_hash="a" * 64,
            runtime_version="4.0.0",
            expires_at="2026-08-19T12:00:00Z",
            rolling_version=1,
            published_revision=1,
        )
        self.assertEqual(value["expiration"], ("winnow-expires-at", "2026-08-19T12:00:00.000Z"))

    def publisher(self, events, *, requests=None, marker_verifier=None, retry_delays=(0,), retry_delay=lambda _seconds: None, core=None):
        requests = list(requests or [(200, {"expiresAt": EXPIRY})])

        def request_json(url, method, body=None, *, headers=None, timeout=30):
            events.append(("request", method, url, body, headers))
            item = requests.pop(0)
            if isinstance(item, BaseException):
                raise item
            return item

        def upload(url, html, headers=None, *, timeout=30):
            events.append(("upload", url, len(html), headers))

        def verify(url, expected):
            events.append(("verify", url, dict(expected)))
            if marker_verifier:
                marker_verifier(url, expected)

        return HereNowPublisher(
            core=core or FakeCore(events),
            image_verifier=lambda _seed: {"scope": "currentRound", "images": 4, "uniqueImages": 4, "verified": []},
            request_json=request_json,
            upload=upload,
            marker_verifier=verify,
            retry_delays_seconds=retry_delays,
            retry_delay=retry_delay,
        )

    def test_create_persists_claim_before_final_compile_and_receipt_is_allowlisted(self):
        events = []
        core = FakeCore(events)
        publisher = self.publisher(events, core=core)
        observed_expirations = []

        def persist(created):
            self.assertEqual(created.claim_token, CLAIM)
            events.append("persist-claim")

        publication = publisher.create(
            seed(),
            persist_claim=persist,
            published_revision=1,
            expected_markers=lambda expires_at: (observed_expirations.append(expires_at) or markers()),
        )
        self.assertEqual(events[:5], ["validate", "build-provisional", "create", "persist-claim", "build-final"])
        self.assertEqual(observed_expirations, [EXPIRY])
        self.assertTrue(core.require_claim_token)
        self.assertEqual(core.client_header, "winnow-remote/1")
        self.assertEqual(publication.pending_version.original_expires_at, EXPIRY)
        receipt = publisher.public_receipt(publication, seed()).as_dict()
        self.assertEqual(set(receipt), {"siteUrl", "expiresAt", "sessionId", "seedHash", "roundNumber", "publishedRevision"})
        self.assertNotIn("claim", json.dumps(receipt).lower())
        self.assertNotIn(CLAIM, repr(publication))
        self.assertNotIn(CLAIM, repr(publication.pending_version))

    def test_claim_persistence_failure_does_not_compile_final_upload_or_leak_secret(self):
        events = []
        publisher = self.publisher(events)

        def fail(_created):
            raise RuntimeError(f"database rejected {CLAIM}")

        with self.assertRaisesRegex(HereNowError, "unable to persist publication secret") as raised:
            publisher.create(seed(), persist_claim=fail, published_revision=1, expected_markers=markers())
        self.assertNotIn(CLAIM, str(raised.exception))
        self.assertNotIn("build-final", events)
        self.assertFalse(any(isinstance(event, tuple) and event[0] == "upload" for event in events))

    def test_create_requires_same_sized_final_html_before_upload(self):
        events = []
        publisher = self.publisher(events, core=FakeCore(events, final_html_delta=1))
        with self.assertRaisesRegex(HereNowError, "byte lengths differ"):
            publisher.create(seed(), persist_claim=lambda _created: events.append("persist-claim"), published_revision=1, expected_markers=markers())
        self.assertFalse(any(isinstance(event, tuple) and event[0] == "upload" for event in events))

    def test_update_persists_pending_before_upload_and_preserves_original_expiration(self):
        events = []
        update_response = {
            "upload": {
                "versionId": "version-2",
                "uploads": [{"path": "index.html", "url": "https://upload.example/update", "headers": {}}],
                "finalizeUrl": "https://finalize.example/update",
            }
        }
        publisher = self.publisher(events, requests=[(200, update_response), (200, {"publishStatus": {"expiresAt": EXPIRY}})])
        pending_values = []
        publication = publisher.update(
            seed(),
            slug="demo",
            claim_token=CLAIM,
            site_url="https://demo.here.now/",
            original_expires_at=EXPIRY,
            persist_pending=lambda pending: (pending_values.append(pending), events.append("persist-pending")),
            published_revision=2,
            expected_markers=markers(2),
        )
        self.assertEqual(pending_values[0].version_id, "version-2")
        request_index = next(index for index, event in enumerate(events) if isinstance(event, tuple) and event[:2] == ("request", "PUT"))
        persist_index = events.index("persist-pending")
        upload_index = next(index for index, event in enumerate(events) if isinstance(event, tuple) and event[0] == "upload")
        self.assertLess(request_index, persist_index)
        self.assertLess(persist_index, upload_index)
        self.assertEqual(publication.pending_version.original_expires_at, EXPIRY)
        request_body = next(event[3] for event in events if isinstance(event, tuple) and event[:2] == ("request", "PUT"))
        self.assertEqual(request_body["claimToken"], CLAIM)
        self.assertEqual(request_body["files"][0]["contentType"], HERE_NOW_CONTENT_TYPE)
        self.assertNotIn(CLAIM, json.dumps(publisher.public_receipt(publication, seed()).as_dict()))

    def test_update_rejects_expiration_extension_before_persisting_pending(self):
        events = []
        publisher = self.publisher(events, requests=[(200, {"expiresAt": "2026-08-20T12:00:00.000Z"})])
        with self.assertRaisesRegex(HereNowError, "changed the original expiration"):
            publisher.update(
                seed(), slug="demo", claim_token=CLAIM, site_url="https://demo.here.now/", original_expires_at=EXPIRY,
                persist_pending=lambda _pending: events.append("persist-pending"), published_revision=2, expected_markers=markers(2),
            )
        self.assertNotIn("persist-pending", events)

    def test_ambiguous_finalize_reconciles_without_reissuing_the_update(self):
        events = []
        publisher = self.publisher(events, requests=[TimeoutError("connection dropped after commit")])
        publication = publisher.create(seed(), persist_claim=lambda _created: events.append("persist-claim"), published_revision=1, expected_markers=markers())
        self.assertTrue(publication.reconciled_after_ambiguous_finalize)
        self.assertEqual(sum(1 for event in events if isinstance(event, tuple) and event[:2] == ("request", "POST")), 1)
        self.assertEqual(sum(1 for event in events if isinstance(event, tuple) and event[0] == "upload"), 1)

    def test_marker_retry_uses_injected_delays_without_sleeping(self):
        events = []
        attempts = {"count": 0}
        delays = []

        def eventually(_url, _expected):
            attempts["count"] += 1
            if attempts["count"] < 3:
                raise HereNowError("stale marker")

        publisher = self.publisher(
            events,
            marker_verifier=eventually,
            retry_delays=(0, 5, 15),
            retry_delay=delays.append,
        )
        publication = publisher.create(seed(), persist_claim=lambda _created: None, published_revision=1, expected_markers=markers())
        self.assertFalse(publication.reconciled_after_ambiguous_finalize)
        self.assertEqual(delays, [5, 15])

    def test_compiled_html_limit_is_enforced(self):
        events = []
        core = FakeCore(events)
        core.build_html = lambda _seed, *, expires_at=None: b"x" * (MAX_REMOTE_COMPILED_HTML_BYTES + 1)
        publisher = self.publisher(events, core=core)
        with self.assertRaisesRegex(HereNowError, "compiled HTML exceeds"):
            publisher.create(seed(), persist_claim=lambda _created: None, published_revision=1, expected_markers=markers())


if __name__ == "__main__":
    unittest.main()
