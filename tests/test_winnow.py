from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("portable_winnow", ROOT / "scripts" / "winnow.py")
assert SPEC and SPEC.loader
winnow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(winnow)


def fixture() -> dict:
    return json.loads((ROOT / "fixtures" / "synthetic-seed.json").read_text(encoding="utf-8"))


class ProtocolTests(unittest.TestCase):
    def test_synthetic_seed_validates(self):
        seed = fixture()
        self.assertIs(winnow.validate_seed(seed), seed)
        self.assertEqual(len(seed["research"]["candidates"]), 16)

    def test_raw_html_is_rejected(self):
        seed = fixture()
        seed["research"]["summary"] = "<script>alert(1)</script>"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_unsourced_title_mismatch_is_rejected(self):
        seed = fixture()
        seed["presentations"][0]["blocks"][0]["text"] = "A different title"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_missing_source_reference_is_rejected(self):
        seed = fixture()
        seed["presentations"][0]["blocks"][1]["sourceId"] = "source-missing"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_low_coverage_is_rejected(self):
        seed = fixture()
        for candidate in seed["research"]["candidates"]:
            for factor_id in list(candidate["facts"])[2:]:
                candidate["facts"][factor_id] = "unknown"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_build_is_self_contained_and_hashable(self):
        seed = fixture()
        html = winnow.build_html(seed, expires_at="2026-08-09T12:00:00Z").decode("utf-8")
        self.assertNotIn("__WINNOW_", html)
        self.assertIn(winnow.seed_hash(seed), html)
        self.assertIn('content="2026-08-09T12:00:00.000Z"', html)
        self.assertIn("connect-src 'none'", html)
        self.assertIn('meta name="winnow-session-id"', html)
        self.assertNotIn("fetch(", html)

    def test_continuation_inspection_requires_schema(self):
        continuation = {
            "protocol": "winnow.continuation",
            "schemaVersion": 1,
            "parent": {"sessionId": "session-1", "seedHash": "a" * 64, "researchAsOf": "2026-08-08T12:00:00Z"},
            "query": "test",
            "activePatterns": [],
            "factorWeights": {"price": 1},
            "verdictHistory": [],
            "seenCandidateIds": [],
            "unresolvedNotes": [],
            "reasons": ["user_requested"],
        }
        summary = winnow.inspect_continuation(continuation)
        self.assertEqual(summary["reasons"], ["user_requested"])
        continuation["reasons"] = []
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_continuation(continuation)


class PublisherTests(unittest.TestCase):
    def test_publish_is_anonymous_new_site_and_discards_claim_token(self):
        base_url = "https://mock.here.now"
        requests: list[tuple[str, str, dict | None, dict | None]] = []
        uploaded: list[bytes] = []

        def fake_json(url, method, body=None, *, headers=None, timeout=30):
            requests.append((method, url, body, headers))
            if method == "POST" and url.endswith("/api/v1/publish"):
                return 200, {
                    "slug": "fresh-synthetic-site",
                    "siteUrl": base_url + "/site",
                    "expiresAt": "2026-08-09T12:00:00.000Z",
                    "anonymous": True,
                    "claimToken": "must-never-appear-in-cli-output",
                    "upload": {
                        "versionId": "version-1",
                        "uploads": [{"path": "index.html", "method": "PUT", "url": base_url + "/upload", "headers": {"Content-Type": "text/html; charset=utf-8"}}],
                        "finalizeUrl": base_url + "/finalize",
                    },
                }
            return 200, {"success": True}

        def fake_upload(url, html, headers=None, *, timeout=30):
            requests.append(("PUT", url, None, headers))
            uploaded.append(html)

        def fake_fetch(url, expected_session_id, *, allow_http=False, timeout=30):
            requests.append(("GET", url, None, None))

        with patch.object(winnow, "_http_json", side_effect=fake_json), patch.object(winnow, "_http_upload", side_effect=fake_upload), patch.object(winnow, "_fetch_live", side_effect=fake_fetch):
            result = winnow.publish(fixture(), endpoint=base_url + "/api/v1/publish")

        self.assertEqual(result["siteUrl"], base_url + "/site")
        self.assertNotIn("claimToken", json.dumps(result))
        self.assertIn(b'content="2026-08-09T12:00:00.000Z"', uploaded[0])
        self.assertEqual([method for method, _url, _body, _headers in requests], ["POST", "PUT", "POST", "GET"])
        for _method, _url, _body, headers in requests:
            self.assertNotIn("Authorization", headers or {})
        create_body = requests[0][2]
        self.assertNotIn("claimToken", create_body)
        self.assertNotIn("hash", create_body["files"][0])


if __name__ == "__main__":
    unittest.main()
