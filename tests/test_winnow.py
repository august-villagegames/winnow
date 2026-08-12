from __future__ import annotations

import base64
import copy
import datetime as dt
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("portable_winnow", ROOT / "scripts" / "winnow.py")
assert SPEC and SPEC.loader
winnow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(winnow)


def fixture(name: str = "synthetic-seed.json") -> dict:
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


def verified_image(url: str) -> dict:
    return {
        "requestedUrl": url,
        "url": url,
        "contentType": "image/png",
        "bytes": 1,
        "sha256": "a" * 64,
    }


class RepositoryChecks(unittest.TestCase):
    def test_winnow_skill_copies_are_identical(self):
        canonical = (ROOT / ".agents" / "skills" / "winnow" / "SKILL.md").read_bytes()
        claude = (ROOT / ".claude" / "skills" / "winnow" / "SKILL.md").read_bytes()
        self.assertEqual(claude, canonical, "Claude and agent Winnow skills must stay byte-for-byte identical")


class ProtocolTests(unittest.TestCase):
    def test_valid_round_one(self):
        seed = fixture()
        self.assertIs(winnow.validate_seed(seed), seed)
        self.assertEqual(seed["schemaVersion"], 3)
        self.assertEqual(len(seed["round"]["options"]), 6)

    def test_valid_round_two_continuation_and_successor(self):
        continuation = fixture("synthetic-continuation.json")
        successor = fixture("synthetic-successor-seed.json")
        self.assertIs(winnow.validate_continuation(continuation), continuation)
        self.assertIs(winnow.validate_successor(continuation, successor), successor)

    def test_profile_exclusions_are_required_and_propagate_to_successors(self):
        seed = fixture()
        seed.pop("profileExclusions")
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

        seed = fixture()
        seed["runtimeVersion"] = "3.0.1"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

        for field in ["parentProfileExclusions", "profileExclusions"]:
            with self.subTest(field=field):
                continuation = fixture("synthetic-continuation.json")
                continuation.pop(field)
                with self.assertRaises(winnow.ValidationError):
                    winnow.validate_continuation(continuation)

        for invalid in ["not-an-array", [""], ["same", "same"], ["x" * 501]]:
            with self.subTest(invalid=invalid):
                seed = fixture()
                seed["profileExclusions"] = invalid
                with self.assertRaises(winnow.ValidationError):
                    winnow.validate_seed(seed)

        continuation = fixture("synthetic-continuation.json")
        successor = fixture("synthetic-successor-seed.json")
        exclusion = '{"direction":"include","factorId":"covers","polarity":"like","value":true}'
        continuation["profileExclusions"] = [exclusion]
        successor["profileExclusions"] = [exclusion]
        self.assertIs(winnow.validate_successor(continuation, successor), successor)

        successor["profileExclusions"] = []
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_successor(continuation, successor)

        continuation = fixture("synthetic-continuation.json")
        continuation["parentProfileExclusions"] = [exclusion]
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_continuation(continuation)

    def test_unknown_keys_are_rejected_at_every_protocol_level(self):
        cases = [
            (lambda value: value.update({"extra": True})),
            (lambda value: value["session"].update({"extra": True})),
            (lambda value: value["round"].update({"extra": True})),
            (lambda value: value["round"]["factors"][0].update({"extra": True})),
            (lambda value: value["round"]["options"][0].update({"extra": True})),
            (lambda value: value["round"]["options"][0]["values"][0].update({"extra": True})),
            (lambda value: value["round"]["sources"][0].update({"extra": True})),
        ]
        for mutate in cases:
            with self.subTest(mutate=mutate):
                seed = fixture()
                mutate(seed)
                with self.assertRaises(winnow.ValidationError):
                    winnow.validate_seed(seed)

        continuation_cases = [
            lambda value: value.update({"extra": True}),
            lambda value: value["parent"].update({"extra": True}),
            lambda value: value["session"].update({"extra": True}),
            lambda value: value["completedRounds"][0].update({"extra": True}),
            lambda value: value["completedRounds"][0]["verdicts"][0].update({"extra": True}),
        ]
        for mutate in continuation_cases:
            with self.subTest(mutate=mutate):
                continuation = fixture("synthetic-continuation.json")
                mutate(continuation)
                with self.assertRaises(winnow.ValidationError):
                    winnow.validate_continuation(continuation)

    def test_wrong_raw_datatype_is_rejected(self):
        seed = fixture()
        seed["round"]["options"][0]["values"][0]["value"] = "1780"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_invalid_display_combinations_are_rejected(self):
        for display in [
            {"style": "currency", "currency": "EUR"},
            {"style": "duration", "unit": "fortnight"},
            {"style": "boolean"},
        ]:
            with self.subTest(display=display):
                seed = fixture()
                seed["round"]["factors"][0]["display"] = display
                with self.assertRaises(winnow.ValidationError):
                    winnow.validate_seed(seed)

    def test_missing_or_extra_factor_values_are_rejected(self):
        seed = fixture()
        seed["round"]["options"][0]["values"].pop()
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_image_policy_requires_images_or_explicit_non_visual_exception(self):
        seed = fixture()
        seed["round"]["options"][0].pop("image")
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

        seed = fixture()
        seed["session"]["imagePolicy"] = {"mode": "notApplicable", "reason": "Synthetic text-only comparison fixture."}
        for option in seed["round"]["options"]:
            option.pop("image")
        self.assertIs(winnow.validate_seed(seed), seed)

        seed["round"]["options"][0]["image"] = {
            "url": "https://example.com/sofas/northline.png",
            "alt": "Northline 3-seat sofa",
            "sourceId": "source-sofa-1",
        }
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

        seed = fixture()
        seed["round"]["options"][0]["values"][0]["factorId"] = "not-declared"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_missing_source_and_unsafe_url_are_rejected(self):
        seed = fixture()
        seed["round"]["options"][0]["values"][0]["sourceId"] = "missing-source"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_multiple_images_are_limited_and_must_use_one_shape(self):
        seed = fixture()
        seed["round"]["options"][0].pop("image")
        seed["round"]["options"][0]["images"] = [
            {"url": "https://example.com/sofas/northline-1.png", "alt": "Northline front view", "sourceId": "source-sofa-1"},
            {"url": "https://example.com/sofas/northline-2.png", "alt": "Northline detail view", "sourceId": "source-sofa-1"},
        ]
        self.assertIs(winnow.validate_seed(seed), seed)

        seed = fixture()
        seed["round"]["options"][0].pop("image")
        seed["round"]["options"][0]["images"] = [
            {"url": f"https://example.com/sofas/northline-{index}.png", "alt": f"Northline view {index}", "sourceId": "source-sofa-1"}
            for index in range(6)
        ]
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

        seed = fixture()
        seed["round"]["options"][0].pop("image")
        seed["round"]["options"][0]["images"] = [
            {"url": "https://example.com/sofas/northline-1.png", "alt": "Northline front view", "sourceId": "source-sofa-1"},
        ]
        seed["round"]["options"][0]["image"] = seed["round"]["options"][0]["images"][0]
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_cdn_hosted_image_is_allowed_and_verified(self):
        seed = fixture()
        seed["round"]["options"][0]["image"]["url"] = "https://cdn.example.net/sofas/northline.png"
        self.assertIs(winnow.validate_seed(seed), seed)
        self.assertIn(winnow.seed_hash(seed), winnow.build_html(seed).decode("utf-8"))

        image_bytes = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")

        class Headers(dict):
            def get_content_type(self):
                return self["Content-Type"].split(";", 1)[0]

        class Response:
            status = 200
            headers = Headers({"Content-Type": "image/png", "Content-Length": str(len(image_bytes))})

            def __init__(self, url):
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return self.url

            def read(self, _limit):
                return image_bytes

        def open_image(request, *, timeout):
            return Response(request.full_url)

        with patch.object(winnow.urllib.request, "urlopen", side_effect=open_image) as fetch:
            result = winnow.verify_image_urls(seed)

        self.assertEqual(result["images"], 6)
        self.assertEqual(result["uniqueImages"], 6)
        self.assertEqual(fetch.call_count, 6)
        item = next(item for item in result["verified"] if item["url"] == "https://cdn.example.net/sofas/northline.png")
        self.assertEqual(item["requestedUrl"], "https://cdn.example.net/sofas/northline.png")
        self.assertEqual(item["sha256"], hashlib.sha256(image_bytes).hexdigest())

    def test_image_verification_only_fetches_current_round_images(self):
        seed = fixture("synthetic-successor-seed.json")
        historical_urls = []
        for option_index, option in enumerate(seed["history"][0]["options"]):
            url = f"https://history.example.invalid/sofas/{option_index}.png"
            option["image"]["url"] = url
            historical_urls.append(url)
        current_urls = [option["image"]["url"] for option in seed["round"]["options"]]

        def fetch_image(url, *, timeout):
            if url in historical_urls:
                raise ValueError("historical image should not be fetched")
            return {"url": url, "contentType": "image/png", "bytes": 1}

        with patch.object(winnow, "_fetch_image", side_effect=fetch_image) as fetch:
            result = winnow.verify_image_urls(seed)

        self.assertEqual(result["images"], 4)
        self.assertEqual(result["uniqueImages"], 4)
        self.assertEqual([item["requestedUrl"] for item in result["verified"]], current_urls)
        self.assertEqual({call.args[0] for call in fetch.call_args_list}, set(current_urls))

    def test_image_verification_deduplicates_current_round_urls(self):
        seed = fixture()
        duplicate_url = seed["round"]["options"][0]["image"]["url"]
        seed["round"]["options"][1]["image"]["url"] = duplicate_url

        with patch.object(
            winnow,
            "_fetch_image",
            return_value={"url": duplicate_url, "contentType": "image/png", "bytes": 1},
        ) as fetch:
            result = winnow.verify_image_urls(seed)

        self.assertEqual(result["images"], 6)
        self.assertEqual(result["uniqueImages"], 5)
        self.assertEqual(fetch.call_count, 5)

    def test_image_verification_receipt_cache_hit_and_expiration(self):
        seed = fixture()
        now = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            receipt_root = Path(directory)
            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)) as fetch:
                first = winnow.verify_image_urls(seed, receipt_root=receipt_root, now=now)
            self.assertEqual(first["scope"], "currentRound")
            self.assertFalse(first["cacheHit"])
            self.assertTrue(first["receiptStored"])
            self.assertEqual(first["receiptExpiresAt"], "2026-08-12T12:00:00.000Z")
            self.assertEqual(fetch.call_count, 6)

            with patch.object(winnow, "_fetch_image", side_effect=AssertionError("cache hit should not fetch")) as fetch:
                second = winnow.verify_image_urls(seed, receipt_root=receipt_root, now=now + dt.timedelta(hours=1))
            self.assertTrue(second["cacheHit"])
            self.assertFalse(second["receiptStored"])
            self.assertEqual(second["receiptExpiresAt"], first["receiptExpiresAt"])
            fetch.assert_not_called()

            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)) as fetch:
                expired = winnow.verify_image_urls(
                    seed,
                    receipt_root=receipt_root,
                    now=now + winnow.VERIFICATION_RECEIPT_TTL,
                )
            self.assertFalse(expired["cacheHit"])
            self.assertTrue(expired["receiptStored"])
            self.assertEqual(fetch.call_count, 6)

    def test_image_verification_receipt_manifest_changes_are_cache_misses(self):
        seed = fixture()
        now = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            receipt_root = Path(directory)
            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)):
                winnow.verify_image_urls(seed, receipt_root=receipt_root, now=now)

            changed = copy.deepcopy(seed)
            changed["round"]["options"][0]["image"]["url"] = "https://example.com/changed.png"
            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)) as fetch:
                result = winnow.verify_image_urls(changed, receipt_root=receipt_root, now=now + dt.timedelta(hours=1))
            self.assertFalse(result["cacheHit"])
            self.assertEqual(fetch.call_count, 6)

            added = copy.deepcopy(seed)
            original = added["round"]["options"][0].pop("image")
            added["round"]["options"][0]["images"] = [
                original,
                {"url": "https://example.com/extra.png", "alt": "Northline detail", "sourceId": "source-sofa-1"},
            ]
            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)) as fetch:
                result = winnow.verify_image_urls(added, receipt_root=receipt_root, now=now + dt.timedelta(hours=2))
            self.assertFalse(result["cacheHit"])
            self.assertEqual(result["images"], 7)
            self.assertEqual(fetch.call_count, 7)

            reordered = copy.deepcopy(added)
            reordered["round"]["options"][0]["images"].reverse()
            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)) as fetch:
                result = winnow.verify_image_urls(reordered, receipt_root=receipt_root, now=now + dt.timedelta(hours=3))
            self.assertFalse(result["cacheHit"])
            self.assertEqual(fetch.call_count, 7)

    def test_image_verification_rejects_mismatched_receipt_metadata(self):
        seed = fixture()
        now = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            receipt_root = Path(directory)
            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)):
                winnow.verify_image_urls(seed, receipt_root=receipt_root, now=now)
            path = winnow._receipt_path(seed, receipt_root)
            for field, value in [("sessionId", "other-session"), ("roundNumber", 2), ("version", 999)]:
                receipt = json.loads(path.read_text(encoding="utf-8"))
                receipt[field] = value
                path.write_text(json.dumps(receipt), encoding="utf-8")
                with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)) as fetch:
                    result = winnow.verify_image_urls(seed, receipt_root=receipt_root, now=now + dt.timedelta(hours=1))
                self.assertFalse(result["cacheHit"])
                self.assertEqual(fetch.call_count, 6)

    def test_not_applicable_image_verification_does_not_fetch_or_store(self):
        seed = fixture()
        seed["session"]["imagePolicy"] = {"mode": "notApplicable", "reason": "Text-only test fixture."}
        for option in seed["round"]["options"]:
            option.pop("image")
        with tempfile.TemporaryDirectory() as directory, patch.object(winnow, "_fetch_image") as fetch:
            result = winnow.verify_image_urls(seed, receipt_root=Path(directory))
        self.assertFalse(result["cacheHit"])
        self.assertFalse(result["receiptStored"])
        self.assertIsNone(result["receiptExpiresAt"])
        fetch.assert_not_called()

    def test_injected_receipt_time_must_be_timezone_aware(self):
        with self.assertRaises(ValueError):
            winnow.verify_image_urls(fixture(), now=dt.datetime(2026, 8, 11, 12, 0))

    def test_receipt_write_failure_falls_back_to_fresh_verification(self):
        seed = fixture()
        with tempfile.TemporaryDirectory() as directory, patch.object(
            winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)
        ) as fetch, patch.object(winnow, "_write_receipt", side_effect=OSError("receipt unavailable")):
            result = winnow.verify_image_urls(seed, receipt_root=Path(directory))
        self.assertFalse(result["cacheHit"])
        self.assertFalse(result["receiptStored"])
        self.assertIsNone(result["receiptExpiresAt"])
        self.assertEqual(fetch.call_count, 6)

    def test_malformed_receipts_are_pruned_without_blocking_verification(self):
        seed = fixture()
        with tempfile.TemporaryDirectory() as directory:
            receipt_root = Path(directory)
            malformed = receipt_root / "orphan" / "round-1.json"
            malformed.parent.mkdir(parents=True)
            malformed.write_text("not-json", encoding="utf-8")
            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)) as fetch:
                result = winnow.verify_image_urls(seed, receipt_root=receipt_root)
            self.assertTrue(result["receiptStored"])
            self.assertEqual(fetch.call_count, 6)
            self.assertFalse(malformed.exists())

    def test_image_verification_rejects_non_image_bytes(self):
        seed = fixture()
        seed["round"]["options"][0]["image"]["url"] = "https://cdn.example.net/sofas/northline.png"

        class Headers(dict):
            def get_content_type(self):
                return self["Content-Type"].split(";", 1)[0]

        class Response:
            status = 200
            headers = Headers({"Content-Type": "image/png", "Content-Length": "15"})

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def geturl(self):
                return "https://cdn.example.net/sofas/northline.png"

            def read(self, _limit):
                return b"<html>not image"

        with patch.object(winnow.urllib.request, "urlopen", return_value=Response()):
            with self.assertRaises(winnow.ValidationError):
                winnow.verify_image_urls(seed)

        seed = fixture()
        seed["round"]["sources"][0]["url"] = "https://user:password@example.com/sofas/northline"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_raw_html_and_control_characters_are_rejected(self):
        for value in ["<script>alert(1)</script>", "Northline\x00sofa"]:
            seed = fixture()
            seed["session"]["title"] = value
            with self.subTest(value=value), self.assertRaises(winnow.ValidationError):
                winnow.validate_seed(seed)

    def test_size_limits_are_rejected(self):
        seed = fixture()
        seed["session"]["requirements"] = ["one", "two", "three", "four", "five", "six"]
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

        seed = fixture()
        seed["round"]["factors"].append(copy.deepcopy(seed["round"]["factors"][0]))
        seed["round"]["factors"][-1]["id"] = "factor-seven"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

        seed = fixture()
        seed["round"]["options"] = seed["round"]["options"][:3]
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_primary_factor_and_lineage_rules(self):
        seed = fixture()
        seed["round"]["factors"] = seed["round"]["factors"][1:]
        for option in seed["round"]["options"]:
            option["values"] = option["values"][1:]
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

        seed = fixture("synthetic-successor-seed.json")
        seed["history"][0]["number"] = 2
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

        seed = fixture("synthetic-successor-seed.json")
        seed["round"]["factors"][0]["display"] = {"style": "decimal"}
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_verdict_integrity_and_option_reuse_are_rejected(self):
        continuation = fixture("synthetic-continuation.json")
        continuation["completedRounds"][0]["verdicts"].pop()
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_continuation(continuation)

        continuation = fixture("synthetic-continuation.json")
        continuation["completedRounds"][0]["verdicts"][0]["optionId"] = continuation["completedRounds"][0]["verdicts"][1]["optionId"]
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_continuation(continuation)

        continuation = fixture("synthetic-continuation.json")
        successor = fixture("synthetic-successor-seed.json")
        successor["round"]["options"][0]["id"] = "sofa-1"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_successor(continuation, successor)

        successor = fixture("synthetic-successor-seed.json")
        successor["history"][0]["options"][0]["title"] = successor["round"]["options"][0]["title"]
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_successor(continuation, successor)

    def test_successor_must_preserve_completed_rounds_and_session(self):
        continuation = fixture("synthetic-continuation.json")
        successor = fixture("synthetic-successor-seed.json")
        successor["session"]["title"] = "Changed title"
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_successor(continuation, successor)

        successor = fixture("synthetic-successor-seed.json")
        successor["history"] = []
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_successor(continuation, successor)

    def test_build_is_self_contained(self):
        html = winnow.build_html(fixture(), expires_at="2026-08-09T12:00:00Z").decode("utf-8")
        self.assertNotIn("__WINNOW_", html)
        self.assertIn(winnow.seed_hash(fixture()), html)
        self.assertIn('content="2026-08-09T12:00:00.000Z"', html)
        self.assertIn("data:font/woff2;base64,", html)
        self.assertIn("Generate a better round", html)
        self.assertIn("Image unavailable", html)
        self.assertIn("imagePolicy", html)
        self.assertIn("profileExclusions", html)
        self.assertIn("data-profile-toggle", html)
        self.assertIn("rotate-ccw", html)
        self.assertIn("connect-src 'none'", html)
        self.assertNotIn("fetch(", html)

    def test_build_compiles_multiple_images_for_the_runtime_carousel(self):
        seed = fixture()
        seed["round"]["options"][0].pop("image")
        seed["round"]["options"][0]["images"] = [
            {"url": "https://example.com/sofas/northline-1.png", "alt": "Northline front view", "sourceId": "source-sofa-1"},
            {"url": "https://example.com/sofas/northline-2.png", "alt": "Northline detail view", "sourceId": "source-sofa-1"},
        ]
        html = winnow.build_html(seed).decode("utf-8")
        self.assertIn("data-carousel", html)
        self.assertIn(winnow.seed_hash(seed), html)

    def test_later_round_publish_requires_continuation(self):
        with self.assertRaises(winnow.ValidationError):
            winnow.publish(fixture("synthetic-successor-seed.json"), endpoint="https://mock.here.now/api/v1/publish")

    def test_local_build_command_is_not_available(self):
        with self.assertRaises(SystemExit) as raised:
            winnow.main(["build", "seed.json", "/tmp/winnow-index.html"])
        self.assertEqual(raised.exception.code, 2)


class PublisherTests(unittest.TestCase):
    def _publish_with_fake_site(
        self,
        seed: dict,
        *,
        receipt_root: Path,
        now: dt.datetime,
        create_error: Exception | None = None,
        upload_error: Exception | None = None,
        finalize_error: Exception | None = None,
        live_error: Exception | None = None,
    ) -> dict:
        base_url = "https://mock.here.now"

        def fake_json(url, method, body=None, *, headers=None, timeout=30):
            if method == "POST" and url == base_url + "/api/v1/publish":
                if create_error is not None:
                    raise create_error
                return 200, {
                    "siteUrl": base_url + "/site",
                    "expiresAt": "2026-08-12T12:00:00.000Z",
                    "anonymous": True,
                    "upload": {
                        "versionId": "version-1",
                        "uploads": [{"path": "index.html", "url": base_url + "/upload", "headers": {}}],
                        "finalizeUrl": base_url + "/finalize",
                    },
                }
            if method == "POST" and url == base_url + "/finalize" and finalize_error is not None:
                raise finalize_error
            return 200, {"success": True}

        def fake_upload(url, html, headers=None, *, timeout=30):
            if upload_error is not None:
                raise upload_error

        def fake_live(url, expected_session_id, *, allow_http=False, timeout=30):
            if live_error is not None:
                raise live_error

        with patch.object(winnow, "_http_json", side_effect=fake_json), patch.object(winnow, "_http_upload", side_effect=fake_upload), patch.object(winnow, "_fetch_live", side_effect=fake_live):
            return winnow.publish(
                seed,
                endpoint=base_url + "/api/v1/publish",
                receipt_root=receipt_root,
                now=now,
            )

    def test_publish_image_verification_is_a_hard_gate(self):
        with patch.object(winnow, "verify_image_urls", side_effect=winnow.ValidationError(["image verification failed"])) as verify, patch.object(winnow, "_http_json") as request:
            with self.assertRaises(winnow.ValidationError):
                winnow.publish(fixture(), endpoint="https://mock.here.now/api/v1/publish")
        verify.assert_called_once_with(fixture(), receipt_root=None, now=None)
        request.assert_not_called()

    def test_diagnostic_verification_is_reused_by_publish_and_success_deletes_receipt(self):
        seed = fixture()
        now = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            receipt_root = Path(directory)
            receipt_path = winnow._receipt_path(seed, receipt_root)
            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)) as fetch:
                winnow.verify_image_urls(seed, receipt_root=receipt_root, now=now)
                result = self._publish_with_fake_site(seed, receipt_root=receipt_root, now=now + dt.timedelta(hours=1))
        self.assertEqual(result["siteUrl"], "https://mock.here.now/site")
        self.assertEqual(fetch.call_count, 6)
        self.assertFalse(receipt_path.exists())

    def test_upload_failure_retains_receipt_and_retry_reuses_it(self):
        seed = fixture()
        now = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            receipt_root = Path(directory)
            receipt_path = winnow._receipt_path(seed, receipt_root)
            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)) as fetch:
                with self.assertRaises(winnow.PublishError):
                    self._publish_with_fake_site(
                        seed,
                        receipt_root=receipt_root,
                        now=now,
                        upload_error=winnow.PublishError("upload failed"),
                    )
                self.assertTrue(receipt_path.exists())
                self._publish_with_fake_site(seed, receipt_root=receipt_root, now=now + dt.timedelta(hours=1))
        self.assertEqual(fetch.call_count, 6)
        self.assertFalse(receipt_path.exists())

    def test_changed_manifest_after_failed_publish_requires_fresh_verification(self):
        seed = fixture()
        now = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            receipt_root = Path(directory)
            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)) as fetch:
                with self.assertRaises(winnow.PublishError):
                    self._publish_with_fake_site(
                        seed,
                        receipt_root=receipt_root,
                        now=now,
                        upload_error=winnow.PublishError("upload failed"),
                    )
                changed = copy.deepcopy(seed)
                changed["round"]["options"][0]["image"]["url"] = "https://example.com/sofas/changed.png"
                self._publish_with_fake_site(changed, receipt_root=receipt_root, now=now + dt.timedelta(hours=1))
        self.assertEqual(fetch.call_count, 12)

    def test_failed_hosted_verification_retains_receipt(self):
        seed = fixture()
        now = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            receipt_root = Path(directory)
            receipt_path = winnow._receipt_path(seed, receipt_root)
            with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)):
                with self.assertRaises(winnow.PublishError):
                    self._publish_with_fake_site(
                        seed,
                        receipt_root=receipt_root,
                        now=now,
                        live_error=winnow.PublishError("hosted verification failed"),
                    )
            self.assertTrue(receipt_path.exists())

    def test_create_and_finalize_failures_retain_receipt(self):
        now = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)
        for failure_name, error in [
            ("create_error", winnow.PublishError("create failed")),
            ("finalize_error", winnow.PublishError("finalize failed")),
        ]:
            with self.subTest(failure_name=failure_name), tempfile.TemporaryDirectory() as directory:
                seed = fixture()
                receipt_root = Path(directory)
                receipt_path = winnow._receipt_path(seed, receipt_root)
                with patch.object(winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)):
                    with self.assertRaises(winnow.PublishError):
                        self._publish_with_fake_site(
                            seed,
                            receipt_root=receipt_root,
                            now=now,
                            **{failure_name: error},
                        )
                self.assertTrue(receipt_path.exists())

    def test_receipt_deletion_failure_does_not_fail_publication(self):
        seed = fixture()
        now = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory, patch.object(
            winnow, "_fetch_image", side_effect=lambda url, *, timeout: verified_image(url)
        ), patch.object(winnow, "_delete_receipt", side_effect=OSError("receipt unavailable")):
            result = self._publish_with_fake_site(seed, receipt_root=Path(directory), now=now)
        self.assertEqual(result["roundNumber"], 1)

    def test_publish_is_anonymous_and_returns_round_number_without_claim_token(self):
        base_url = "https://mock.here.now"
        seed = fixture()
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

        with patch.object(winnow, "verify_image_urls", return_value={"images": 6, "uniqueImages": 6, "verified": []}), patch.object(winnow, "_http_json", side_effect=fake_json), patch.object(winnow, "_http_upload", side_effect=fake_upload), patch.object(winnow, "_fetch_live", side_effect=fake_fetch), patch.object(winnow, "_delete_receipt") as delete_receipt:
            result = winnow.publish(seed, endpoint=base_url + "/api/v1/publish")

        self.assertEqual(result["siteUrl"], base_url + "/site")
        self.assertEqual(result["roundNumber"], 1)
        delete_receipt.assert_called_once_with(seed, receipt_root=None)
        self.assertNotIn("claimToken", json.dumps(result))
        self.assertIn(b"content=\"2026-08-09T12:00:00.000Z\"", uploaded[0])
        self.assertEqual([method for method, _url, _body, _headers in requests], ["POST", "PUT", "POST", "GET"])
        for _method, _url, _body, headers in requests:
            self.assertNotIn("Authorization", headers or {})


if __name__ == "__main__":
    unittest.main()
