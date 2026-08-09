#!/usr/bin/env python3
"""Compile and anonymously publish portable Winnow sessions.

This file intentionally uses only the Python standard library.  It is safe to
copy into an agent's temporary workspace together with the committed runtime.
The publisher never reads credentials, sends Authorization, updates a slug, or
returns here.now's anonymous claim token.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import math
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


PROTOCOL = "winnow.portable-session"
SCHEMA_VERSION = 1
RUNTIME_VERSION = "1.0.0"
CONTINUATION_PROTOCOL = "winnow.continuation"
EXPORT_PROTOCOL = "winnow.export"
PUBLISH_ENDPOINT = "https://here.now/api/v1/publish"
CONTENT_TYPE = "text/html; charset=utf-8"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
HTML_RE = re.compile(r"<[^>]+>")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")
EXPIRATION_PLACEHOLDER = "0000-00-00T00:00:00.000Z"
SEED_TOKEN = "__WINNOW_SEED_BASE64__"
HASH_TOKEN = "__WINNOW_SEED_HASH__"
SESSION_TOKEN = "__WINNOW_SESSION_ID__"
RUNTIME_TOKEN = "__WINNOW_RUNTIME_VERSION__"


class ValidationError(ValueError):
    """A user-facing collection of schema failures."""

    def __init__(self, errors: Iterable[str]):
        self.errors = list(errors)
        super().__init__("; ".join(self.errors))


class PublishError(RuntimeError):
    pass


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def seed_hash(seed: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(seed)).hexdigest()


def _plain(value: Any, path: str, *, max_length: int = 4000, required: bool = True) -> None:
    if not isinstance(value, str):
        raise ValidationError([f"{path}: expected text"])
    if required and not value.strip():
        raise ValidationError([f"{path}: must not be empty"])
    if len(value) > max_length:
        raise ValidationError([f"{path}: exceeds {max_length} characters"])
    if HTML_RE.search(value):
        raise ValidationError([f"{path}: raw HTML is not allowed"])
    if any(ord(char) < 9 or (13 < ord(char) < 32) for char in value):
        raise ValidationError([f"{path}: control characters are not allowed"])


def _id(value: Any, path: str) -> None:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValidationError([f"{path}: invalid identifier"])


def _iso(value: Any, path: str) -> None:
    if not isinstance(value, str) or not ISO_RE.fullmatch(value):
        raise ValidationError([f"{path}: expected an ISO-8601 timestamp with timezone"])
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError([f"{path}: invalid timestamp"]) from exc


def _https(value: Any, path: str) -> urllib.parse.ParseResult:
    if not isinstance(value, str):
        raise ValidationError([f"{path}: expected URL"])
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValidationError([f"{path}: only credential-free HTTPS URLs are allowed"])
    if HTML_RE.search(value):
        raise ValidationError([f"{path}: raw HTML is not allowed"])
    return parsed


def _object(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError([f"{path}: expected an object"])
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError([f"{path}: expected an array"])
    return value


def _required(obj: dict[str, Any], key: str, path: str) -> Any:
    if key not in obj:
        raise ValidationError([f"{path}.{key}: is required"])
    return obj[key]


def _unique_ids(values: list[Any], path: str) -> None:
    ids = []
    for index, value in enumerate(values):
        item = _object(value, f"{path}[{index}]")
        _id(_required(item, "id", f"{path}[{index}]"), f"{path}[{index}].id")
        ids.append(item["id"])
    if len(set(ids)) != len(ids):
        raise ValidationError([f"{path}: duplicate ids are not allowed"])


def _string_array(value: Any, path: str, *, min_length: int = 0, max_length: int | None = None) -> list[str]:
    items = _array(value, path)
    if len(items) < min_length:
        raise ValidationError([f"{path}: requires at least {min_length} items"])
    if max_length is not None and len(items) > max_length:
        raise ValidationError([f"{path}: allows at most {max_length} items"])
    seen: set[str] = set()
    result: list[str] = []
    for index, item in enumerate(items):
        _plain(item, f"{path}[{index}]", max_length=4000)
        if item in seen:
            raise ValidationError([f"{path}: duplicate value {item}"])
        seen.add(item)
        result.append(item)
    return result


def _primitive(value: Any, path: str, *, allow_unknown: bool = True) -> None:
    if allow_unknown and value == "unknown":
        return
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if not math.isfinite(value):
            raise ValidationError([f"{path}: number must be finite"])
        return
    if isinstance(value, str):
        _plain(value, path, max_length=4000)
        return
    if isinstance(value, list) and all(isinstance(item, str) and item.strip() for item in value):
        for index, item in enumerate(value):
            _plain(item, f"{path}[{index}]", max_length=4000)
        return
    raise ValidationError([f"{path}: expected string, number, boolean, string array, or unknown"])


def _validate_source(source: dict[str, Any], index: int) -> None:
    path = f"research.sources[{index}]"
    _id(_required(source, "id", path), f"{path}.id")
    _plain(_required(source, "title", path), f"{path}.title", max_length=4000)
    _https(_required(source, "url", path), f"{path}.url")
    _iso(_required(source, "retrievedAt", path), f"{path}.retrievedAt")
    if "note" in source:
        _plain(source["note"], f"{path}.note", max_length=4000)


def _validate_factor(factor: dict[str, Any], index: int) -> None:
    path = f"research.factors[{index}]"
    _id(_required(factor, "id", path), f"{path}.id")
    _plain(_required(factor, "label", path), f"{path}.label", max_length=120)
    _plain(_required(factor, "description", path), f"{path}.description", max_length=4000)
    if factor.get("valueType") not in {None, "boolean", "category", "number", "text"}:
        raise ValidationError([f"{path}.valueType: unsupported value type"])


def _validate_candidate(candidate: dict[str, Any], index: int, source_ids: set[str], factor_ids: set[str]) -> None:
    path = f"research.candidates[{index}]"
    _id(_required(candidate, "id", path), f"{path}.id")
    _plain(_required(candidate, "name", path), f"{path}.name", max_length=200)
    _plain(_required(candidate, "summary", path), f"{path}.summary", max_length=4000)
    candidate_sources = _string_array(_required(candidate, "sourceIds", path), f"{path}.sourceIds", min_length=1)
    for source_id in candidate_sources:
        _id(source_id, f"{path}.sourceIds")
        if source_id not in source_ids:
            raise ValidationError([f"{path}.sourceIds: missing source {source_id}"])
    facts = _object(_required(candidate, "facts", path), f"{path}.facts")
    for factor_id, value in facts.items():
        _id(factor_id, f"{path}.facts.{factor_id}")
        if factor_id not in factor_ids:
            raise ValidationError([f"{path}.facts: missing factor {factor_id}"])
        _primitive(value, f"{path}.facts.{factor_id}")


def _source_host(source: dict[str, Any]) -> str:
    return urllib.parse.urlparse(source["url"]).hostname or ""


def _validate_block(block: dict[str, Any], path: str, candidate: dict[str, Any], sources: dict[str, dict[str, Any]]) -> None:
    block_type = block.get("type")
    if block_type not in {"image", "title", "text", "metric-grid", "badge-list", "link"}:
        raise ValidationError([f"{path}.type: unsupported card block"])
    if block_type == "title":
        _plain(_required(block, "text", path), f"{path}.text", max_length=300)
        if block["text"] != candidate["name"]:
            raise ValidationError([f"{path}.text: card title must match the researched candidate name"])
        return

    def cited_source(source_id: Any) -> dict[str, Any]:
        _id(source_id, f"{path}.sourceId")
        if source_id not in sources:
            raise ValidationError([f"{path}.sourceId: source is not in the research pack"])
        if source_id not in candidate["sourceIds"]:
            raise ValidationError([f"{path}.sourceId: source is not cited by this candidate"])
        return sources[source_id]

    if block_type in {"image", "text", "badge-list", "link"}:
        source = cited_source(_required(block, "sourceId", path))
        if block_type == "image":
            _plain(_required(block, "alt", path), f"{path}.alt", max_length=300)
            image_url = _https(_required(block, "url", path), f"{path}.url")
            if image_url.hostname != _source_host(source):
                raise ValidationError([f"{path}.url: image host must match its cited source host"])
        elif block_type == "text":
            _plain(_required(block, "text", path), f"{path}.text")
            if block.get("tone") not in {None, "default", "muted", "positive", "caution"}:
                raise ValidationError([f"{path}.tone: unsupported tone"])
        elif block_type == "badge-list":
            _string_array(_required(block, "items", path), f"{path}.items", min_length=1, max_length=12)
            for item_index, item in enumerate(block["items"]):
                _plain(item, f"{path}.items[{item_index}]", max_length=120)
        else:
            _plain(_required(block, "label", path), f"{path}.label", max_length=120)
            link_url = _https(_required(block, "url", path), f"{path}.url")
            if link_url.hostname != _source_host(source):
                raise ValidationError([f"{path}.url: link host must match its cited source host"])
        return

    items = _array(_required(block, "items", path), f"{path}.items")
    if not items or len(items) > 8:
        raise ValidationError([f"{path}.items: requires 1–8 metrics"])
    for item_index, item_value in enumerate(items):
        item = _object(item_value, f"{path}.items[{item_index}]")
        _plain(_required(item, "label", f"{path}.items[{item_index}]"), f"{path}.items[{item_index}].label", max_length=120)
        _plain(_required(item, "value", f"{path}.items[{item_index}]"), f"{path}.items[{item_index}].value", max_length=200)
        if "unit" in item:
            _plain(item["unit"], f"{path}.items[{item_index}].unit", max_length=40)
        if "sourceId" in item:
            cited_source(item["sourceId"])
        elif item["value"].lower() != "unknown":
            raise ValidationError([f"{path}.items[{item_index}]: non-unknown metrics require a sourceId"])


def _validate_presentation(presentation: dict[str, Any], index: int, candidates: dict[str, dict[str, Any]], sources: dict[str, dict[str, Any]]) -> None:
    path = f"presentations[{index}]"
    candidate_id = _required(presentation, "candidateId", path)
    _id(candidate_id, f"{path}.candidateId")
    if candidate_id not in candidates:
        raise ValidationError([f"{path}.candidateId: candidate does not exist"])
    presentation_sources = _string_array(_required(presentation, "sourceIds", path), f"{path}.sourceIds", min_length=1)
    for source_id in presentation_sources:
        if source_id not in sources or source_id not in candidates[candidate_id]["sourceIds"]:
            raise ValidationError([f"{path}.sourceIds: source {source_id} is not valid for this candidate"])
    blocks = _array(_required(presentation, "blocks", path), f"{path}.blocks")
    if not blocks or len(blocks) > 16:
        raise ValidationError([f"{path}.blocks: requires 1–16 blocks"])
    for block_index, block_value in enumerate(blocks):
        _validate_block(_object(block_value, f"{path}.blocks[{block_index}]"), f"{path}.blocks[{block_index}]", candidates[candidate_id], sources)
    if not any(block.get("type") != "title" and ("sourceId" in block or block.get("type") == "metric-grid") for block in blocks):
        raise ValidationError([f"{path}: must contain at least one evidence block"])


def validate_seed(seed: Any) -> dict[str, Any]:
    errors: list[str] = []
    try:
        root = _object(seed, "seed")
        if root.get("protocol") != PROTOCOL:
            errors.append("seed.protocol: unsupported protocol")
        if root.get("schemaVersion") != SCHEMA_VERSION:
            errors.append("seed.schemaVersion: unsupported schema version")
        if not isinstance(root.get("runtimeVersion"), str) or root.get("runtimeVersion") != RUNTIME_VERSION:
            errors.append(f"seed.runtimeVersion: expected {RUNTIME_VERSION}")
        _id(_required(root, "sessionId", "seed"), "seed.sessionId")
        _iso(_required(root, "createdAt", "seed"), "seed.createdAt")
        _plain(_required(root, "query", "seed"), "seed.query", max_length=1000)

        research = _object(_required(root, "research", "seed"), "seed.research")
        _iso(_required(research, "asOf", "seed.research"), "seed.research.asOf")
        _plain(_required(research, "summary", "seed.research"), "seed.research.summary")
        assumptions = _string_array(_required(research, "assumptions", "seed.research"), "seed.research.assumptions", max_length=12)
        for index, assumption in enumerate(assumptions):
            _plain(assumption, f"seed.research.assumptions[{index}]", max_length=4000)
        sources_list = _array(_required(research, "sources", "seed.research"), "seed.research.sources")
        factors_list = _array(_required(research, "factors", "seed.research"), "seed.research.factors")
        candidates_list = _array(_required(research, "candidates", "seed.research"), "seed.research.candidates")
        if not 1 <= len(sources_list) <= 80:
            errors.append("seed.research.sources: requires 1–80 sources")
        if not 6 <= len(factors_list) <= 10:
            errors.append("seed.research.factors: portable research requires 6–10 comparable factors")
        if not 12 <= len(candidates_list) <= 24:
            errors.append("seed.research.candidates: portable research requires 12–24 candidates")
        _unique_ids(sources_list, "seed.research.sources")
        _unique_ids(factors_list, "seed.research.factors")
        _unique_ids(candidates_list, "seed.research.candidates")
        for index, source in enumerate(sources_list):
            _validate_source(_object(source, f"seed.research.sources[{index}]"), index)
        for index, factor in enumerate(factors_list):
            _validate_factor(_object(factor, f"seed.research.factors[{index}]"), index)
        source_map = {source["id"]: source for source in sources_list}
        factor_map = {factor["id"]: factor for factor in factors_list}
        for index, candidate in enumerate(candidates_list):
            _validate_candidate(_object(candidate, f"seed.research.candidates[{index}]"), index, set(source_map), set(factor_map))
        candidate_map = {candidate["id"]: candidate for candidate in candidates_list}

        covered = sum(1 for candidate in candidates_list for factor_id in factor_map if candidate["facts"].get(factor_id, "unknown") != "unknown")
        if covered / (len(candidates_list) * len(factor_map)) < 0.70:
            errors.append("seed.research: at least 70% of candidate/factor pairs need usable facts")

        presentations = _array(_required(root, "presentations", "seed"), "seed.presentations")
        if len(presentations) != len(candidates_list):
            errors.append("seed.presentations: exactly one presentation is required per candidate")
        presentation_ids = []
        for index, item in enumerate(presentations):
            if not isinstance(item, dict):
                errors.append(f"seed.presentations[{index}]: expected an object")
                continue
            presentation_ids.append({"id": item.get("candidateId")})
        if presentation_ids:
            _unique_ids(presentation_ids, "seed.presentations")
        for index, presentation in enumerate(presentations):
            _validate_presentation(_object(presentation, f"seed.presentations[{index}]"), index, candidate_map, source_map)
        if set(item["candidateId"] for item in presentations) != set(candidate_map):
            errors.append("seed.presentations: presentations must cover every candidate exactly once")

        initial = _object(_required(root, "initialRound", "seed"), "seed.initialRound")
        initial_candidates = _string_array(_required(initial, "candidateIds", "seed.initialRound"), "seed.initialRound.candidateIds", min_length=4, max_length=4)
        if len(set(initial_candidates)) != 4 or any(candidate_id not in candidate_map for candidate_id in initial_candidates):
            errors.append("seed.initialRound.candidateIds: must contain four existing candidates")
        initial_factors = _string_array(_required(initial, "factorIds", "seed.initialRound"), "seed.initialRound.factorIds", min_length=1, max_length=6)
        if any(factor_id not in factor_map for factor_id in initial_factors):
            errors.append("seed.initialRound.factorIds: every factor must exist in research")
        _plain(_required(initial, "generationExplanation", "seed.initialRound"), "seed.initialRound.generationExplanation", max_length=4000)

        strategy = _object(_required(root, "localStrategy", "seed"), "seed.localStrategy")
        expected_strategy = {
            "roundSize": 4,
            "factorLimit": 6,
            "factorWeightStep": 1.5,
            "factorWeightMin": 0.25,
            "factorWeightMax": 4,
            "relevanceWeight": 0.75,
            "diversityWeight": 0.15,
            "evidenceWeight": 0.1,
        }
        for key, expected in expected_strategy.items():
            if strategy.get(key) != expected:
                errors.append(f"seed.localStrategy.{key}: expected {expected}")
    except ValidationError as exc:
        errors.extend(exc.errors)
    except (KeyError, TypeError, ValueError) as exc:
        errors.append(f"seed: malformed structure ({exc})")
    if errors:
        raise ValidationError(errors)
    return root


def validate_continuation(value: Any) -> dict[str, Any]:
    errors: list[str] = []
    try:
        root = _object(value, "continuation")
        if root.get("protocol") != CONTINUATION_PROTOCOL:
            errors.append("continuation.protocol: unsupported protocol")
        if root.get("schemaVersion") != SCHEMA_VERSION:
            errors.append("continuation.schemaVersion: unsupported schema version")
        parent = _object(_required(root, "parent", "continuation"), "continuation.parent")
        for key in ("sessionId", "seedHash", "researchAsOf"):
            _plain(_required(parent, key, "continuation.parent"), f"continuation.parent.{key}", max_length=200)
        _iso(parent["researchAsOf"], "continuation.parent.researchAsOf")
        if "url" in parent:
            _https(parent["url"], "continuation.parent.url")
        _plain(_required(root, "query", "continuation"), "continuation.query", max_length=1000)
        _string_array(_required(root, "activePatterns", "continuation"), "continuation.activePatterns", max_length=64)
        weights = _object(_required(root, "factorWeights", "continuation"), "continuation.factorWeights")
        for factor_id, weight in weights.items():
            _id(factor_id, f"continuation.factorWeights.{factor_id}")
            if not isinstance(weight, (int, float)) or not 0.25 <= weight <= 4:
                errors.append(f"continuation.factorWeights.{factor_id}: expected a weight from 0.25 to 4")
        history = _array(_required(root, "verdictHistory", "continuation"), "continuation.verdictHistory")
        for index, item_value in enumerate(history):
            item = _object(item_value, f"continuation.verdictHistory[{index}]")
            _id(_required(item, "candidateId", f"continuation.verdictHistory[{index}]"), f"continuation.verdictHistory[{index}].candidateId")
            _plain(_required(item, "candidateName", f"continuation.verdictHistory[{index}]"), f"continuation.verdictHistory[{index}].candidateName", max_length=200)
            if item.get("decision") not in {"like", "dislike", "skip"} or not isinstance(item.get("roundIndex"), int) or item["roundIndex"] < 0:
                errors.append(f"continuation.verdictHistory[{index}]: invalid verdict")
        _string_array(_required(root, "seenCandidateIds", "continuation"), "continuation.seenCandidateIds", max_length=64)
        _string_array(_required(root, "unresolvedNotes", "continuation"), "continuation.unresolvedNotes", max_length=64)
        reasons = _string_array(_required(root, "reasons", "continuation"), "continuation.reasons", min_length=1, max_length=6)
        allowed = {"user_requested", "free_text_unapplied", "all_disliked", "missing_evidence", "corpus_exhausted", "research_stale"}
        if any(reason not in allowed for reason in reasons):
            errors.append("continuation.reasons: contains an unsupported reason")
    except ValidationError as exc:
        errors.extend(exc.errors)
    if errors:
        raise ValidationError(errors)
    return root


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValidationError([f"{path}: cannot read valid JSON ({exc})"]) from exc


def _utc_expiration(value: str | None) -> str:
    if not value:
        return EXPIRATION_PLACEHOLDER
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(dt.timezone.utc)
    except ValueError as exc:
        raise PublishError("here.now returned an invalid expiration timestamp") from exc
    return parsed.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def build_html(seed: dict[str, Any], *, expires_at: str | None = None, template_path: Path | None = None) -> bytes:
    validate_seed(seed)
    template = template_path or Path(__file__).resolve().parents[1] / "assets" / "runtime.html"
    try:
        html = template.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublishError(f"runtime template is unavailable: {exc}") from exc
    digest = seed_hash(seed)
    encoded = base64.b64encode(canonical_json(seed)).decode("ascii")
    replacements = {
        SEED_TOKEN: encoded,
        HASH_TOKEN: digest,
        SESSION_TOKEN: seed["sessionId"],
        RUNTIME_TOKEN: RUNTIME_VERSION,
        EXPIRATION_PLACEHOLDER: _utc_expiration(expires_at),
    }
    for token, replacement in replacements.items():
        if token == EXPIRATION_PLACEHOLDER and token not in html:
            raise PublishError("runtime template is missing the fixed-width expiration placeholder")
        if token != EXPIRATION_PLACEHOLDER and token not in html:
            raise PublishError(f"runtime template is missing placeholder {token}")
        html = html.replace(token, replacement)
    if any(token in html for token in (SEED_TOKEN, HASH_TOKEN, SESSION_TOKEN, RUNTIME_TOKEN)):
        raise PublishError("runtime template placeholders were not fully compiled")
    return html.encode("utf-8")


def _http_json(url: str, method: str, body: dict[str, Any] | None = None, *, headers: dict[str, str] | None = None, timeout: float = 30) -> tuple[int, dict[str, Any]]:
    payload = None if body is None else canonical_json(body)
    request_headers = {"Accept": "application/json"}
    if payload is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    request = urllib.request.Request(url, data=payload, headers=request_headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else {}
            return response.status, parsed
    except urllib.error.HTTPError as exc:
        raise PublishError(f"here.now {method} failed with HTTP {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise PublishError(f"here.now {method} failed ({type(exc).__name__})") from None


def _http_upload(url: str, html: bytes, headers: dict[str, Any] | None, *, timeout: float = 30) -> None:
    upload_headers = {str(key): str(value) for key, value in (headers or {}).items() if str(key).lower() != "authorization"}
    upload_headers.setdefault("Content-Type", CONTENT_TYPE)
    request = urllib.request.Request(url, data=html, headers=upload_headers, method="PUT")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            response.read()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError) as exc:
        status = getattr(exc, "code", "network")
        raise PublishError(f"here.now upload failed ({status})") from None


def _fetch_live(url: str, expected_session_id: str, *, allow_http: bool = False, timeout: float = 30) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and not allow_http:
        raise PublishError("here.now returned a non-HTTPS site URL")
    request = urllib.request.Request(url, headers={"Accept": "text/html"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            html = response.read(2_000_000).decode("utf-8", errors="strict")
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, UnicodeDecodeError) as exc:
        raise PublishError(f"live Site verification failed ({getattr(exc, 'code', 'network')})") from None
    if f'name="winnow-session-id" content="{expected_session_id}"' not in html:
        raise PublishError("live Site verification failed: session id mismatch")


def publish(seed: dict[str, Any], *, endpoint: str = PUBLISH_ENDPOINT, allow_http_test: bool = False) -> dict[str, Any]:
    validate_seed(seed)
    html = build_html(seed)
    response_headers = {"X-HereNow-Client": "winnow-portable/1"}
    # The anonymous create response supplies expiresAt.  It is part of the
    # immutable HTML, so omit the optional manifest hash and upload exactly one
    # final version after the response is received.
    status, created = _http_json(endpoint, "POST", {"files": [{"path": "index.html", "size": len(html), "contentType": CONTENT_TYPE}]}, headers=response_headers)
    if status < 200 or status >= 300:
        raise PublishError(f"here.now create failed with HTTP {status}")
    if created.get("anonymous") is not True:
        raise PublishError("here.now did not create an anonymous Site")
    expires_at = created.get("expiresAt")
    if not isinstance(expires_at, str):
        raise PublishError("here.now anonymous response is missing expiresAt")
    upload = _object(created.get("upload"), "publish.upload")
    uploads = _array(upload.get("uploads"), "publish.upload.uploads")
    matching = next((item for item in uploads if isinstance(item, dict) and item.get("path") == "index.html"), None)
    if matching is None:
        raise PublishError("here.now did not return an index.html upload URL")
    upload_url = matching.get("url")
    if not isinstance(upload_url, str) or urllib.parse.urlparse(upload_url).scheme not in {"http", "https"}:
        raise PublishError("here.now returned an invalid upload URL")
    html = build_html(seed, expires_at=expires_at)
    _http_upload(upload_url, html, matching.get("headers"))
    finalize_url = upload.get("finalizeUrl")
    version_id = upload.get("versionId")
    if not isinstance(finalize_url, str) or not isinstance(version_id, str):
        raise PublishError("here.now create response is missing finalize metadata")
    _http_json(finalize_url, "POST", {"versionId": version_id})
    site_url = created.get("siteUrl")
    if not isinstance(site_url, str):
        raise PublishError("here.now create response is missing siteUrl")
    _fetch_live(site_url, seed["sessionId"], allow_http=allow_http_test)
    # Deliberately construct a new result rather than filtering and returning
    # the provider response: claimToken/claimUrl cannot leak from this CLI.
    return {
        "siteUrl": site_url,
        "expiresAt": expires_at,
        "sessionId": seed["sessionId"],
        "seedHash": seed_hash(seed),
    }


def inspect_continuation(value: dict[str, Any]) -> dict[str, Any]:
    validate_continuation(value)
    return {
        "query": value["query"],
        "parentSessionId": value["parent"]["sessionId"],
        "seenCandidates": len(value["seenCandidateIds"]),
        "verdicts": len(value["verdictHistory"]),
        "activePatterns": len(value["activePatterns"]),
        "reasons": value["reasons"],
    }


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Portable Winnow seed compiler and anonymous here.now publisher")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="validate a seed JSON file")
    validate_parser.add_argument("seed", type=Path)

    build_parser = subparsers.add_parser("build", help="build a self-contained HTML session")
    build_parser.add_argument("seed", type=Path)
    build_parser.add_argument("output", type=Path)
    build_parser.add_argument("--expires-at", help="UTC/offset timestamp supplied by a publisher")

    publish_parser = subparsers.add_parser("publish", help="create and publish a new anonymous here.now Site")
    publish_parser.add_argument("seed", type=Path)
    publish_parser.add_argument("--endpoint", default=PUBLISH_ENDPOINT, help=argparse.SUPPRESS)
    publish_parser.add_argument("--allow-http-test", action="store_true", help=argparse.SUPPRESS)

    inspect_parser = subparsers.add_parser("inspect-continuation", help="validate and summarize a continuation package")
    inspect_parser.add_argument("continuation", type=Path)

    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            validate_seed(read_json(args.seed))
            print(json.dumps({"valid": True, "seedHash": seed_hash(read_json(args.seed))}, separators=(",", ":")))
        elif args.command == "build":
            seed = read_json(args.seed)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_bytes(build_html(seed, expires_at=args.expires_at))
            print(json.dumps({"output": str(args.output), "seedHash": seed_hash(seed)}, separators=(",", ":")))
        elif args.command == "publish":
            result = publish(read_json(args.seed), endpoint=args.endpoint, allow_http_test=args.allow_http_test)
            print(json.dumps(result, separators=(",", ":")))
        else:
            print(json.dumps(inspect_continuation(read_json(args.continuation)), separators=(",", ":")))
        return 0
    except (ValidationError, PublishError, OSError) as exc:
        print(f"winnow: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
