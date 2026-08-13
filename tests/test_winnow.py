from __future__ import annotations

import base64
import copy
import importlib.util
import json
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("portable_winnow", ROOT / "scripts" / "winnow.py")
assert SPEC and SPEC.loader
winnow = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(winnow)
SCHEMA = json.loads((ROOT / "references" / "seed.schema.json").read_text(encoding="utf-8"))


def fixture(name: str = "synthetic-seed.json") -> dict:
    return json.loads((ROOT / "fixtures" / name).read_text(encoding="utf-8"))


PNG_BYTES = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class ImageHeaders(dict):
    def get_content_type(self):
        return self.get("Content-Type", "").split(";", 1)[0]


class ImageResponse:
    def __init__(
        self,
        *,
        body: bytes = PNG_BYTES,
        content_type: str | None = "image/png",
        content_length: str | None = None,
        final_url: str = "https://cdn.example.net/image.png",
        status: int = 200,
    ):
        headers = {}
        if content_type is not None:
            headers["Content-Type"] = content_type
        if content_length is not None:
            headers["Content-Length"] = content_length
        self.body = body
        self.headers = ImageHeaders(headers)
        self.final_url = final_url
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.final_url

    def read(self, _limit):
        return self.body


def four_option_seed() -> dict:
    seed = fixture()
    seed["round"]["options"] = seed["round"]["options"][:4]
    return seed


def ten_option_seed() -> dict:
    seed = fixture()
    for option_number in range(7, 11):
        option = copy.deepcopy(seed["round"]["options"][option_number - 7])
        option["id"] = f"sofa-{option_number}"
        option["title"] = f"Additional sofa {option_number}"
        option["optionUrl"]["url"] = f"https://example.com/sofas/additional-{option_number}"
        seed["round"]["options"].append(option)
    return seed


def verified_image(url: str) -> dict:
    return {"url": url, "contentType": "image/png", "bytes": len(PNG_BYTES)}


class RepositoryChecks(unittest.TestCase):
    def test_winnow_skill_paths_share_canonical_directory(self):
        canonical_dir = ROOT / ".agents" / "skills" / "winnow"
        claude_dir = ROOT / ".claude" / "skills" / "winnow"
        canonical = canonical_dir / "SKILL.md"
        claude = claude_dir / "SKILL.md"
        self.assertTrue(canonical.is_file())
        self.assertTrue(claude_dir.is_symlink(), "Claude skill path must point to the canonical skill directory")
        self.assertEqual(claude_dir.resolve(), canonical_dir.resolve())
        self.assertEqual(claude.read_bytes(), canonical.read_bytes())
        skill_text = canonical.read_text(encoding="utf-8")
        self.assertIn("npx skills add august-villagegames/winnow --skill winnow", skill_text)
        self.assertIn("npx skills update winnow", skill_text)

    def test_schema_declares_winnow_key_uniqueness_boundary(self):
        profile_patterns = SCHEMA["$defs"]["profilePatterns"]
        self.assertEqual(profile_patterns["x-winnow-uniqueItemsBy"], "key")
        self.assertIn("standard JSON Schema uniqueItems keyword", profile_patterns["$comment"])


class ProtocolTests(unittest.TestCase):
    def test_valid_round_one(self):
        seed = fixture()
        self.assertIs(winnow.validate_seed(seed), seed)
        self.assertEqual(seed["schemaVersion"], 4)
        self.assertEqual(len(seed["round"]["options"]), 6)

    def test_round_allows_up_to_ten_options(self):
        seed = ten_option_seed()
        self.assertIs(winnow.validate_seed(seed), seed)
        self.assertEqual(len(seed["round"]["options"]), 10)

        too_many = copy.deepcopy(seed)
        option = copy.deepcopy(too_many["round"]["options"][0])
        option["id"] = "sofa-11"
        option["title"] = "Additional sofa 11"
        option["optionUrl"]["url"] = "https://example.com/sofas/additional-11"
        too_many["round"]["options"].append(option)
        with self.assertRaisesRegex(winnow.ValidationError, "requires 4–10 options"):
            winnow.validate_seed(too_many)

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

    def test_profile_patterns_are_required_and_propagate_to_successors(self):
        seed = fixture()
        seed.pop("profilePatterns")
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

        for field in ["parentProfilePatterns", "profilePatterns"]:
            with self.subTest(field=field):
                continuation = fixture("synthetic-continuation.json")
                continuation.pop(field)
                with self.assertRaises(winnow.ValidationError):
                    winnow.validate_continuation(continuation)

        continuation = fixture("synthetic-continuation.json")
        successor = fixture("synthetic-successor-seed.json")
        successor["profilePatterns"] = []
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_successor(continuation, successor)

        continuation = fixture("synthetic-continuation.json")
        continuation["profilePatterns"][0]["mean"] = 123
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_continuation(continuation)

        pattern = fixture("synthetic-continuation.json")["profilePatterns"][0]
        for invalid in [
            [{**pattern, "key": "not-canonical"}],
            [pattern, copy.deepcopy(pattern)],
            [pattern] * 7,
            [{**pattern, "factorId": "undeclared"}],
            [{**pattern, "direction": "lower"}],
            [{**pattern, "value": True}],
            [{**pattern, "mean": 1}],
        ]:
            with self.subTest(invalid=invalid):
                seed = fixture()
                seed["profilePatterns"] = invalid
                with self.assertRaises(winnow.ValidationError):
                    winnow.validate_seed(seed)

        numeric = {
            "key": winnow._profile_pattern_key("price", "like", "average", None),
            "factorId": "price",
            "polarity": "like",
            "direction": "average",
            "value": None,
            "mean": 1870,
            "supportCount": 2,
            "strength": 1,
        }
        for invalid in [
            {**numeric, "direction": "include", "key": winnow._profile_pattern_key("price", "like", "include", None)},
            {**numeric, "value": 1870, "key": winnow._profile_pattern_key("price", "like", "average", 1870)},
            {**numeric, "mean": None},
            {**numeric, "direction": "higher", "key": winnow._profile_pattern_key("price", "like", "higher", None), "mean": 1870},
        ]:
            with self.subTest(invalid=invalid):
                seed = fixture()
                seed["profilePatterns"] = [invalid]
                with self.assertRaises(winnow.ValidationError):
                    winnow.validate_seed(seed)

        seed = fixture()
        seed["profilePatterns"] = [pattern]
        seed["profileExclusions"] = [pattern["key"]]
        with self.assertRaises(winnow.ValidationError):
            winnow.validate_seed(seed)

    def test_legacy_distinct_numeric_profile_patterns_remain_valid(self):
        seed = fixture()
        lower = {
            "key": winnow._profile_pattern_key("price", "dislike", "lower", None),
            "factorId": "price",
            "polarity": "dislike",
            "direction": "lower",
            "value": None,
            "mean": None,
            "supportCount": 2,
            "strength": 1,
        }
        higher = {
            "key": winnow._profile_pattern_key("price", "like", "higher", None),
            "factorId": "price",
            "polarity": "like",
            "direction": "higher",
            "value": None,
            "mean": None,
            "supportCount": 2,
            "strength": 1,
        }
        seed["profilePatterns"] = [lower, higher]
        self.assertIs(winnow.validate_seed(seed), seed)

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
            lambda value: value["profilePatterns"][0].update({"extra": True}),
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

        seed = fixture()
        seed["round"]["sources"][0]["url"] = "https://user:password@example.com/sofas/northline"
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

        def open_image(request, *, timeout):
            self.assertEqual(request.get_method(), "GET")
            self.assertIn("image/png", request.get_header("Accept"))
            return ImageResponse(
                body=PNG_BYTES,
                content_length=str(len(PNG_BYTES)),
                final_url=request.full_url,
            )

        with patch.object(winnow.urllib.request, "urlopen", side_effect=open_image) as fetch:
            result = winnow.verify_image_urls(seed)

        self.assertEqual(result["images"], 6)
        self.assertEqual(result["uniqueImages"], 6)
        self.assertEqual(fetch.call_count, 6)
        item = next(item for item in result["verified"] if item["url"] == "https://cdn.example.net/sofas/northline.png")
        self.assertEqual(item["contentType"], "image/png")
        self.assertEqual(item["bytes"], len(PNG_BYTES))

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
        self.assertEqual([item["url"] for item in result["verified"]], current_urls)
        self.assertEqual(fetch.call_count, 4)
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
        self.assertEqual([call.args[0] for call in fetch.call_args_list].count(duplicate_url), 1)

    def test_not_applicable_image_verification_does_not_fetch(self):
        seed = fixture()
        seed["session"]["imagePolicy"] = {"mode": "notApplicable", "reason": "Text-only test fixture."}
        for option in seed["round"]["options"]:
            option.pop("image")
        with patch.object(winnow, "_fetch_image") as fetch:
            result = winnow.verify_image_urls(seed)
        self.assertEqual(result, {"scope": "currentRound", "images": 0, "uniqueImages": 0, "verified": []})
        fetch.assert_not_called()

    def test_image_verification_runs_four_distinct_urls_concurrently(self):
        seed = four_option_seed()
        barrier = threading.Barrier(4)
        lock = threading.Lock()
        active = 0
        peak_active = 0

        def fetch_image(url, *, timeout):
            nonlocal active, peak_active
            with lock:
                active += 1
                peak_active = max(peak_active, active)
            try:
                barrier.wait(timeout=2)
                return verified_image(url)
            finally:
                with lock:
                    active -= 1

        with patch.object(winnow, "_fetch_image", side_effect=fetch_image) as fetch:
            result = winnow.verify_image_urls(seed)

        self.assertEqual(fetch.call_count, 4)
        self.assertEqual(peak_active, 4)
        self.assertEqual(result["uniqueImages"], 4)

    def test_image_verification_preserves_first_url_order_after_reverse_completion(self):
        seed = four_option_seed()
        urls = [option["image"]["url"] for option in seed["round"]["options"]]
        positions = {url: index for index, url in enumerate(urls)}
        barrier = threading.Barrier(4)
        releases = [threading.Event() for _url in urls]
        completion_order: list[str] = []
        completion_lock = threading.Lock()

        def fetch_image(url, *, timeout):
            index = positions[url]
            barrier.wait(timeout=2)
            if index == len(urls) - 1:
                releases[index].set()
            if not releases[index].wait(timeout=2):
                raise AssertionError("verification completion chain timed out")
            with completion_lock:
                completion_order.append(url)
            if index:
                releases[index - 1].set()
            return verified_image(url)

        with patch.object(winnow, "_fetch_image", side_effect=fetch_image):
            result = winnow.verify_image_urls(seed)

        self.assertEqual(completion_order, list(reversed(urls)))
        self.assertEqual([item["url"] for item in result["verified"]], urls)

    def test_image_verification_reports_multiple_failures_in_source_order(self):
        seed = four_option_seed()
        urls = [option["image"]["url"] for option in seed["round"]["options"]]
        positions = {url: index for index, url in enumerate(urls)}
        barrier = threading.Barrier(4)
        releases = [threading.Event() for _url in urls]
        completion_order: list[int] = []
        completion_lock = threading.Lock()

        def fetch_image(url, *, timeout):
            index = positions[url]
            barrier.wait(timeout=2)
            if index == len(urls) - 1:
                releases[index].set()
            if not releases[index].wait(timeout=2):
                raise AssertionError("verification failure chain timed out")
            with completion_lock:
                completion_order.append(index)
            if index:
                releases[index - 1].set()
            raise ValueError(f"failure-{index}")

        with patch.object(winnow, "_fetch_image", side_effect=fetch_image):
            with self.assertRaises(winnow.ValidationError) as raised:
                winnow.verify_image_urls(seed)

        self.assertEqual(completion_order, [3, 2, 1, 0])
        self.assertEqual(
            raised.exception.errors,
            [
                f"image verification failed: seed.round.options[{index}].image: failure-{index}"
                for index in range(4)
            ],
        )

    def test_fetch_image_accepts_each_supported_raster_signature(self):
        cases = [
            ("image/png", PNG_BYTES),
            ("image/jpeg", b"\xff\xd8\xff\xe0jpeg"),
            ("image/gif", b"GIF89aimage"),
            ("image/webp", b"RIFF\x04\x00\x00\x00WEBPdata"),
            ("image/avif", b"\x00\x00\x00\x18ftypavifdata"),
        ]
        for content_type, body in cases:
            with self.subTest(content_type=content_type), patch.object(
                winnow.urllib.request,
                "urlopen",
                return_value=ImageResponse(
                    body=body,
                    content_type=content_type,
                    content_length=str(len(body)),
                ),
            ):
                result = winnow._fetch_image("https://cdn.example.net/image")
                self.assertEqual(result["contentType"], content_type)
                self.assertEqual(result["bytes"], len(body))

    def test_fetch_image_rejects_unsafe_or_invalid_responses(self):
        oversized_body = PNG_BYTES + b"x" * winnow.IMAGE_MAX_BYTES
        cases = [
            ("non-2xx status", ImageResponse(status=404), ValueError, "HTTP status 404"),
            ("non-HTTPS redirect", ImageResponse(final_url="http://cdn.example.net/image.png"), winnow.ValidationError, "credential-free HTTPS"),
            ("credentialed redirect", ImageResponse(final_url="https://user:secret@cdn.example.net/image.png"), winnow.ValidationError, "credential-free HTTPS"),
            ("missing content type", ImageResponse(content_type=None), ValueError, "unsupported content type missing"),
            ("unsupported content type", ImageResponse(content_type="text/html"), ValueError, "unsupported content type text/html"),
            ("nonnumeric content length", ImageResponse(content_length="many"), ValueError, "invalid Content-Length"),
            ("negative content length", ImageResponse(content_length="-1"), ValueError, "invalid Content-Length"),
            ("declared body too large", ImageResponse(content_length=str(winnow.IMAGE_MAX_BYTES + 1)), ValueError, "response exceeds"),
            ("actual body too large", ImageResponse(body=oversized_body), ValueError, "response exceeds"),
            ("empty body", ImageResponse(body=b"", content_length="0"), ValueError, "signature was not recognized"),
            ("unrecognized body", ImageResponse(body=b"<html>error</html>", content_type="image/png"), ValueError, "signature was not recognized"),
            ("MIME-signature mismatch", ImageResponse(body=b"GIF89aimage", content_type="image/png"), ValueError, "does not match image/gif bytes"),
        ]
        for label, response, error_type, message in cases:
            with self.subTest(label=label), patch.object(winnow.urllib.request, "urlopen", return_value=response):
                with self.assertRaisesRegex(error_type, message):
                    winnow._fetch_image("https://cdn.example.net/image.png")

    def test_fetch_image_normalizes_http_and_network_failures(self):
        failures = [
            (
                winnow.urllib.error.HTTPError(
                    "https://cdn.example.net/image.png",
                    403,
                    "Forbidden",
                    ImageHeaders(),
                    None,
                ),
                "HTTP status 403",
            ),
            (winnow.urllib.error.URLError("DNS failed"), r"network error \(URLError\)"),
            (TimeoutError("timed out"), r"network error \(TimeoutError\)"),
        ]
        for failure, message in failures:
            with self.subTest(failure=type(failure).__name__), patch.object(
                winnow.urllib.request,
                "urlopen",
                side_effect=failure,
            ):
                with self.assertRaisesRegex(ValueError, message):
                    winnow._fetch_image("https://cdn.example.net/image.png")
            if isinstance(failure, winnow.urllib.error.HTTPError):
                failure.close()

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
        self.assertIn("data-viewer-open", html)
        self.assertIn("data-image-viewer", html)
        self.assertIn("data-viewer-prev", html)
        self.assertIn("data-viewer-next", html)
        self.assertIn("data-viewer-close", html)
        self.assertIn(winnow.seed_hash(seed), html)

    def test_build_compiles_viewer_for_singular_image(self):
        html = winnow.build_html(fixture()).decode("utf-8")
        self.assertIn("data-viewer-open", html)
        self.assertIn("data-viewer-image", html)
        self.assertIn("object-fit: contain", html)
        self.assertNotIn("__WINNOW_", html)

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

        def fake_live(url, expected_session_id, expected_seed_hash, expected_runtime_version, expected_expires_at, *, allow_http=False, timeout=30):
            if live_error is not None:
                raise live_error

        with patch.object(winnow, "_http_json", side_effect=fake_json), patch.object(winnow, "_http_upload", side_effect=fake_upload), patch.object(winnow, "_fetch_live", side_effect=fake_live):
            return winnow.publish(
                seed,
                endpoint=base_url + "/api/v1/publish",
            )

    def test_publish_image_verification_is_a_hard_gate(self):
        with (
            patch.object(
                winnow,
                "verify_image_urls",
                side_effect=winnow.ValidationError(["image verification failed"]),
            ) as verify,
            patch.object(winnow, "_http_json") as request,
            patch.object(winnow, "_http_upload") as upload,
            patch.object(winnow, "_fetch_live") as fetch_live,
        ):
            with self.assertRaises(winnow.ValidationError):
                winnow.publish(fixture(), endpoint="https://mock.here.now/api/v1/publish")
        verify.assert_called_once_with(fixture())
        request.assert_not_called()
        upload.assert_not_called()
        fetch_live.assert_not_called()

    def test_fetch_live_requires_exact_deployment_markers(self):
        expected = {
            "session": "synthetic-sofa-session",
            "seed": "a" * 64,
            "runtime": "4.0.0",
            "expires": "2026-08-12T12:00:00.000Z",
        }

        def hosted_html(**overrides):
            values = {**expected, **overrides}
            return "".join(
                [
                    f'<meta name="winnow-session-id" content="{values["session"]}">',
                    f'<meta name="winnow-seed-hash" content="{values["seed"]}">',
                    f'<meta name="winnow-runtime-version" content="{values["runtime"]}">',
                    f'<meta name="winnow-expires-at" content="{values["expires"]}">',
                ]
            ).encode("utf-8")

        class Response:
            status = 200

            def __init__(self, body: bytes):
                self.body = body

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, _limit):
                return self.body

        with patch.object(
            winnow.urllib.request,
            "urlopen",
            side_effect=lambda _request, timeout: Response(hosted_html()),
        ):
            winnow._fetch_live(
                "https://mock.here.now/site",
                expected["session"],
                expected["seed"],
                expected["runtime"],
                expected["expires"],
            )

        for field, wrong_value in [("session", "other-session"), ("seed", "b" * 64), ("runtime", "3.0.1"), ("expires", "2026-08-13T12:00:00.000Z")]:
            with self.subTest(field=field), patch.object(
                winnow.urllib.request,
                "urlopen",
                side_effect=lambda _request, timeout, value={field: wrong_value}: Response(hosted_html(**value)),
            ):
                with self.assertRaises(winnow.PublishError):
                    winnow._fetch_live(
                        "https://mock.here.now/site",
                        expected["session"],
                        expected["seed"],
                        expected["runtime"],
                        expected["expires"],
                    )

        semantically_equivalent_but_inexact = hosted_html().replace(
            f'<meta name="winnow-session-id" content="{expected["session"]}">'.encode(),
            f'<meta content="{expected["session"]}" name="winnow-session-id">'.encode(),
        )
        with patch.object(
            winnow.urllib.request,
            "urlopen",
            side_effect=lambda _request, timeout: Response(semantically_equivalent_but_inexact),
        ):
            with self.assertRaisesRegex(winnow.PublishError, "session id mismatch"):
                winnow._fetch_live(
                    "https://mock.here.now/site",
                    expected["session"],
                    expected["seed"],
                    expected["runtime"],
                    expected["expires"],
                )

    def test_fetch_live_rejects_non_https_site_urls(self):
        with patch.object(winnow.urllib.request, "urlopen") as request:
            with self.assertRaises(winnow.PublishError):
                winnow._fetch_live(
                    "http://mock.here.now/site",
                    "synthetic-sofa-session",
                    "a" * 64,
                    "4.0.0",
                    "2026-08-12T12:00:00.000Z",
                )
        request.assert_not_called()

    def test_failed_publish_retry_verifies_images_again(self):
        seed = fixture()
        verified = lambda url, *, timeout: {"url": url, "contentType": "image/png", "bytes": 1}
        with patch.object(winnow, "_fetch_image", side_effect=verified) as fetch:
            with self.assertRaises(winnow.PublishError):
                self._publish_with_fake_site(seed, upload_error=winnow.PublishError("upload failed"))
            result = self._publish_with_fake_site(seed)
        self.assertEqual(result["siteUrl"], "https://mock.here.now/site")
        self.assertEqual(fetch.call_count, 12)

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

        def fake_fetch(url, expected_session_id, expected_seed_hash, expected_runtime_version, expected_expires_at, *, allow_http=False, timeout=30):
            requests.append(("GET", url, None, None))

        with patch.object(winnow, "verify_image_urls", return_value={"scope": "currentRound", "images": 6, "uniqueImages": 6, "verified": []}), patch.object(winnow, "_http_json", side_effect=fake_json), patch.object(winnow, "_http_upload", side_effect=fake_upload), patch.object(winnow, "_fetch_live", side_effect=fake_fetch):
            result = winnow.publish(seed, endpoint=base_url + "/api/v1/publish")

        self.assertEqual(result["siteUrl"], base_url + "/site")
        self.assertEqual(result["roundNumber"], 1)
        self.assertEqual(result["imageVerification"], {"scope": "currentRound", "images": 6, "uniqueImages": 6})
        self.assertEqual(set(result["timingsMs"]), {"validation", "imageVerification", "sitePublication", "total"})
        self.assertTrue(all(isinstance(value, int) and value >= 0 for value in result["timingsMs"].values()))
        self.assertNotIn("cache", json.dumps(result).lower())
        self.assertNotIn("receipt", json.dumps(result).lower())
        self.assertNotIn("claimToken", json.dumps(result))
        self.assertEqual(len(uploaded), 1)
        self.assertIn(b"content=\"2026-08-09T12:00:00.000Z\"", uploaded[0])
        self.assertEqual([method for method, _url, _body, _headers in requests], ["POST", "PUT", "POST", "GET"])
        for _method, _url, _body, headers in requests:
            self.assertNotIn("Authorization", headers or {})


if __name__ == "__main__":
    unittest.main()
