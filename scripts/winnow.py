#!/usr/bin/env python3
"""Validate, compile, and anonymously publish Portable Winnow v3 sessions."""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import datetime as dt
import hashlib
import json
import math
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterable


PROTOCOL = "winnow.portable-session"
SCHEMA_VERSION = 3
RUNTIME_VERSION = "3.0.2"
CONTINUATION_PROTOCOL = "winnow.continuation"
PUBLISH_ENDPOINT = "https://here.now/api/v1/publish"
CONTENT_TYPE = "text/html; charset=utf-8"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")
HTML_RE = re.compile(r"<[^>]+>")
EXPIRATION_PLACEHOLDER = "0000-00-00T00:00:00.000Z"
SEED_TOKEN = "__WINNOW_SEED_BASE64__"
HASH_TOKEN = "__WINNOW_SEED_HASH__"
SESSION_TOKEN = "__WINNOW_SESSION_ID__"
RUNTIME_TOKEN = "__WINNOW_RUNTIME_VERSION__"
CSS_TOKEN = "__WINNOW_CSS__"
CORE_TOKEN = "__WINNOW_CORE_JS__"
UI_TOKEN = "__WINNOW_UI_JS__"
ICONS_TOKEN = "__WINNOW_ICONS__"
FONT_TOKEN = "__WINNOW_FONT_DATA__"
IMAGE_MAX_BYTES = 8 * 1024 * 1024
IMAGE_CONTENT_TYPES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"}


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


def _object(value: Any, path: str, allowed: set[str] | None = None) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError([f"{path}: expected an object"])
    if allowed is not None:
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise ValidationError([f"{path}: unknown properties: {', '.join(unknown)}"])
    return value


def _array(value: Any, path: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValidationError([f"{path}: expected an array"])
    return value


def _required(obj: dict[str, Any], key: str, path: str) -> Any:
    if key not in obj:
        raise ValidationError([f"{path}.{key}: is required"])
    return obj[key]


def _plain(value: Any, path: str, *, max_length: int, trimmed: bool = False) -> str:
    if not isinstance(value, str):
        raise ValidationError([f"{path}: expected plain text"])
    if not value or not value.strip():
        raise ValidationError([f"{path}: must not be empty"])
    if trimmed and value != value.strip():
        raise ValidationError([f"{path}: must be trimmed"])
    if len(value) > max_length:
        raise ValidationError([f"{path}: exceeds {max_length} characters"])
    if HTML_RE.search(value):
        raise ValidationError([f"{path}: raw HTML is not allowed"])
    if any(ord(char) < 9 or (13 < ord(char) < 32) for char in value):
        raise ValidationError([f"{path}: control characters are not allowed"])
    return value


def _id(value: Any, path: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise ValidationError([f"{path}: invalid identifier"])
    return value


def _iso(value: Any, path: str) -> str:
    if not isinstance(value, str) or not ISO_RE.fullmatch(value):
        raise ValidationError([f"{path}: expected an ISO-8601 timestamp with timezone"])
    try:
        dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError([f"{path}: invalid timestamp"]) from exc
    return value


def _https(value: Any, path: str) -> urllib.parse.ParseResult:
    if not isinstance(value, str):
        raise ValidationError([f"{path}: expected URL"])
    if any(ord(char) < 33 for char in value):
        raise ValidationError([f"{path}: control characters are not allowed"])
    parsed = urllib.parse.urlparse(value)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise ValidationError([f"{path}: only credential-free HTTPS URLs are allowed"])
    if HTML_RE.search(value):
        raise ValidationError([f"{path}: raw HTML is not allowed"])
    return parsed


def _unique(values: list[Any], path: str, label: str = "values") -> None:
    if len(set(values)) != len(values):
        raise ValidationError([f"{path}: duplicate {label} are not allowed"])


def _string_array(value: Any, path: str, *, min_items: int = 0, max_items: int | None = None, max_length: int = 100) -> list[str]:
    items = _array(value, path)
    if len(items) < min_items:
        raise ValidationError([f"{path}: requires at least {min_items} items"])
    if max_items is not None and len(items) > max_items:
        raise ValidationError([f"{path}: allows at most {max_items} items"])
    result = [_plain(item, f"{path}[{index}]", max_length=max_length) for index, item in enumerate(items)]
    _unique(result, path, "values")
    return result


def _validate_profile_exclusions(value: Any, path: str) -> list[str]:
    return _string_array(value, path, max_length=500)


def _validate_display(factor: dict[str, Any], path: str) -> dict[str, Any]:
    value_type = factor["valueType"]
    display = _object(factor["display"], f"{path}.display")
    style = display.get("style")
    if value_type == "number":
        if style == "decimal":
            allowed = {"style", "unit"}
            _object(display, f"{path}.display", allowed)
            if "unit" in display and display["unit"] != "nominations":
                raise ValidationError([f"{path}.display.unit: expected nominations"])
        elif style == "currency":
            _object(display, f"{path}.display", {"style", "currency"})
            if display.get("currency") != "USD":
                raise ValidationError([f"{path}.display.currency: only USD is supported"])
        elif style == "percent":
            _object(display, f"{path}.display", {"style"})
        elif style == "duration":
            _object(display, f"{path}.display", {"style", "unit"})
            if display.get("unit") not in {"minute", "hour", "day", "week", "month", "year"}:
                raise ValidationError([f"{path}.display.unit: invalid duration unit"])
        else:
            raise ValidationError([f"{path}.display.style: invalid number display style"])
    elif value_type == "boolean":
        _object(display, f"{path}.display", {"style", "trueLabel", "falseLabel"})
        if style != "boolean":
            raise ValidationError([f"{path}.display.style: expected boolean"])
        _plain(display.get("trueLabel"), f"{path}.display.trueLabel", max_length=100)
        _plain(display.get("falseLabel"), f"{path}.display.falseLabel", max_length=100)
    else:
        _object(display, f"{path}.display", {"style"})
        if style != "text":
            raise ValidationError([f"{path}.display.style: expected text"])
    return display


def _validate_factor(factor: Any, index: int, path_prefix: str) -> dict[str, Any]:
    path = f"{path_prefix}[{index}]"
    factor = _object(factor, path, {"id", "label", "valueType", "display"})
    _id(_required(factor, "id", path), f"{path}.id")
    _plain(_required(factor, "label", path), f"{path}.label", max_length=120, trimmed=True)
    if factor.get("valueType") not in {"number", "boolean", "category", "text"}:
        raise ValidationError([f"{path}.valueType: unsupported value type"])
    _required(factor, "display", path)
    _validate_display(factor, path)
    return factor


def _validate_source(source: Any, index: int, path_prefix: str) -> dict[str, Any]:
    path = f"{path_prefix}[{index}]"
    source = _object(source, path, {"id", "title", "url", "retrievedAt"})
    _id(_required(source, "id", path), f"{path}.id")
    _plain(_required(source, "title", path), f"{path}.title", max_length=200)
    _https(_required(source, "url", path), f"{path}.url")
    _iso(_required(source, "retrievedAt", path), f"{path}.retrievedAt")
    return source


def _source_host(source: dict[str, Any]) -> str:
    return urllib.parse.urlparse(source["url"]).hostname or ""


def _source_ref(value: Any, path: str, source_map: dict[str, dict[str, Any]]) -> None:
    source_id = _id(value, path)
    if source_id not in source_map:
        raise ValidationError([f"{path}: missing source reference {source_id}"])


def _validate_image(image: Any, path: str, sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    image = _object(image, path, {"url", "alt", "sourceId"})
    _https(_required(image, "url", path), f"{path}.url")
    _plain(_required(image, "alt", path), f"{path}.alt", max_length=200)
    source_id = _required(image, "sourceId", path)
    _source_ref(source_id, f"{path}.sourceId", sources)
    return image


def _validate_option(option: Any, index: int, path_prefix: str, factors: list[dict[str, Any]], sources: dict[str, dict[str, Any]]) -> dict[str, Any]:
    path = f"{path_prefix}[{index}]"
    option = _object(option, path, {"id", "title", "primarySourceId", "description", "image", "images", "optionUrl", "values"})
    _id(_required(option, "id", path), f"{path}.id")
    _plain(_required(option, "title", path), f"{path}.title", max_length=200)
    _source_ref(_required(option, "primarySourceId", path), f"{path}.primarySourceId", sources)
    if "description" in option:
        description = _object(option["description"], f"{path}.description", {"text", "sourceId"})
        _plain(_required(description, "text", f"{path}.description"), f"{path}.description.text", max_length=800)
        _source_ref(_required(description, "sourceId", f"{path}.description"), f"{path}.description.sourceId", sources)
    if "image" in option and "images" in option:
        raise ValidationError([f"{path}: use either image or images, not both"])
    if "image" in option:
        _validate_image(option["image"], f"{path}.image", sources)
    if "images" in option:
        images = _array(option["images"], f"{path}.images")
        if not 1 <= len(images) <= 5:
            raise ValidationError([f"{path}.images: requires 1–5 images"])
        for image_index, image in enumerate(images):
            _validate_image(image, f"{path}.images[{image_index}]", sources)
    if "optionUrl" in option:
        option_url = _object(option["optionUrl"], f"{path}.optionUrl", {"url", "sourceId"})
        page_url = _https(_required(option_url, "url", f"{path}.optionUrl"), f"{path}.optionUrl.url")
        _source_ref(_required(option_url, "sourceId", f"{path}.optionUrl"), f"{path}.optionUrl.sourceId", sources)
        if page_url.hostname != _source_host(sources[option_url["sourceId"]]):
            raise ValidationError([f"{path}.optionUrl.url: host must match cited source host"])
    values = _array(_required(option, "values", path), f"{path}.values")
    if len(values) != len(factors):
        raise ValidationError([f"{path}.values: must contain exactly one value for every round factor"])
    factor_map = {factor["id"]: factor for factor in factors}
    seen_factor_ids: set[str] = set()
    for value_index, raw_value in enumerate(values):
        value_path = f"{path}.values[{value_index}]"
        value = _object(raw_value, value_path, {"factorId", "value", "sourceId"})
        factor_id = _id(_required(value, "factorId", value_path), f"{value_path}.factorId")
        if factor_id in seen_factor_ids:
            raise ValidationError([f"{path}.values: duplicate factor {factor_id}"])
        seen_factor_ids.add(factor_id)
        if factor_id not in factor_map:
            raise ValidationError([f"{value_path}.factorId: undeclared factor"])
        _source_ref(_required(value, "sourceId", value_path), f"{value_path}.sourceId", sources)
        raw = value["value"]
        factor_type = factor_map[factor_id]["valueType"]
        if raw == "unknown" or raw is None:
            raise ValidationError([f"{value_path}.value: missing values are not permitted"])
        if factor_type == "number" and (isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(raw)):
            raise ValidationError([f"{value_path}.value: expected a finite number"])
        if factor_type == "boolean" and not isinstance(raw, bool):
            raise ValidationError([f"{value_path}.value: expected boolean"])
        if factor_type in {"category", "text"}:
            _plain(raw, f"{value_path}.value", max_length=400)
    if seen_factor_ids != set(factor_map):
        raise ValidationError([f"{path}.values: missing factor value"])
    return option


def _validate_round(round_value: Any, expected_number: int, completed: bool, path: str) -> dict[str, Any]:
    allowed = {"number", "generatedAt", "factors", "sources", "options"} | ({"verdicts"} if completed else set())
    round_value = _object(round_value, path, allowed)
    if round_value.get("number") != expected_number or not isinstance(round_value.get("number"), int) or isinstance(round_value.get("number"), bool):
        raise ValidationError([f"{path}.number: expected {expected_number}"])
    _iso(_required(round_value, "generatedAt", path), f"{path}.generatedAt")
    factors_raw = _array(_required(round_value, "factors", path), f"{path}.factors")
    if not 1 <= len(factors_raw) <= 6:
        raise ValidationError([f"{path}.factors: requires 1–6 factors"])
    factors = [_validate_factor(value, index, f"{path}.factors") for index, value in enumerate(factors_raw)]
    _unique([factor["id"] for factor in factors], f"{path}.factors", "factor IDs")
    _unique([factor["label"] for factor in factors], f"{path}.factors", "factor labels")
    sources_raw = _array(_required(round_value, "sources", path), f"{path}.sources")
    if not sources_raw:
        raise ValidationError([f"{path}.sources: requires at least one source"])
    sources = [_validate_source(value, index, f"{path}.sources") for index, value in enumerate(sources_raw)]
    _unique([source["id"] for source in sources], f"{path}.sources", "source IDs")
    source_map = {source["id"]: source for source in sources}
    options_raw = _array(_required(round_value, "options", path), f"{path}.options")
    if not 4 <= len(options_raw) <= 6:
        raise ValidationError([f"{path}.options: requires 4–6 options"])
    options = [_validate_option(value, index, f"{path}.options", factors, source_map) for index, value in enumerate(options_raw)]
    _unique([option["id"] for option in options], f"{path}.options", "option IDs")
    if completed:
        verdicts_raw = _array(_required(round_value, "verdicts", path), f"{path}.verdicts")
        if len(verdicts_raw) != len(options):
            raise ValidationError([f"{path}.verdicts: requires exactly one verdict per option"])
        verdicts = []
        seen: set[str] = set()
        option_ids = {option["id"] for option in options}
        for index, raw_verdict in enumerate(verdicts_raw):
            verdict_path = f"{path}.verdicts[{index}]"
            verdict = _object(raw_verdict, verdict_path, {"optionId", "decision"})
            option_id = _id(_required(verdict, "optionId", verdict_path), f"{verdict_path}.optionId")
            if option_id not in option_ids or option_id in seen:
                raise ValidationError([f"{verdict_path}.optionId: missing or duplicate option verdict"])
            if verdict.get("decision") not in {"like", "dislike", "skip"}:
                raise ValidationError([f"{verdict_path}.decision: invalid decision"])
            seen.add(option_id)
            verdicts.append(verdict)
        if seen != option_ids:
            raise ValidationError([f"{path}.verdicts: missing option verdict"])
        round_value["verdicts"] = verdicts
    return round_value


def _normalized_title(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _validate_round_lineage(rounds: list[dict[str, Any]], session: dict[str, Any]) -> None:
    seen_ids: set[str] = set()
    seen_titles: set[str] = set()
    seen_urls: set[str] = set()
    factor_definitions: dict[str, str] = {}
    primary_id = session.get("primaryFactorId")
    for round_value in rounds:
        factors = round_value["factors"]
        factor_map = {factor["id"]: factor for factor in factors}
        if primary_id and primary_id not in factor_map:
            raise ValidationError([f"round {round_value['number']}: primary factor is absent"])
        for factor in factors:
            definition = canonical_json({"label": factor["label"], "valueType": factor["valueType"], "display": factor["display"]}).decode("utf-8")
            previous = factor_definitions.get(factor["id"])
            if previous is not None and previous != definition:
                raise ValidationError([f"factor {factor['id']}: definition changed across rounds"])
            factor_definitions[factor["id"]] = definition
        for option in round_value["options"]:
            if option["id"] in seen_ids:
                raise ValidationError([f"option {option['id']}: ID is reused across rounds"])
            seen_ids.add(option["id"])
            normalized_title = _normalized_title(option["title"])
            if normalized_title in seen_titles:
                raise ValidationError([f"option {option['id']}: normalized title is reused across rounds"])
            seen_titles.add(normalized_title)
            if "optionUrl" in option:
                normalized_url = _canonical_url(option["optionUrl"]["url"])
                if normalized_url in seen_urls:
                    raise ValidationError([f"option {option['id']}: canonical option URL is reused across rounds"])
                seen_urls.add(normalized_url)


def _canonical_url(value: str) -> str:
    parsed = urllib.parse.urlparse(value)
    hostname = (parsed.hostname or "").lower()
    port = parsed.port
    netloc = hostname
    if port and port not in {443}:
        netloc += f":{port}"
    path = parsed.path or "/"
    return urllib.parse.urlunparse(("https", netloc, path, "", parsed.query, ""))


def _validate_session(value: Any, path: str = "seed.session") -> dict[str, Any]:
    value = _object(value, path, {"id", "title", "query", "requirements", "primaryFactorId", "imagePolicy"})
    _id(_required(value, "id", path), f"{path}.id")
    _plain(_required(value, "title", path), f"{path}.title", max_length=120, trimmed=True)
    _plain(_required(value, "query", path), f"{path}.query", max_length=1000)
    _string_array(_required(value, "requirements", path), f"{path}.requirements", max_items=5, max_length=100)
    if "primaryFactorId" in value:
        _id(value["primaryFactorId"], f"{path}.primaryFactorId")
    policy = _object(_required(value, "imagePolicy", path), f"{path}.imagePolicy", {"mode", "reason"})
    mode = policy.get("mode")
    if mode == "required":
        _object(policy, f"{path}.imagePolicy", {"mode"})
    elif mode == "notApplicable":
        _object(policy, f"{path}.imagePolicy", {"mode", "reason"})
        _plain(_required(policy, "reason", f"{path}.imagePolicy"), f"{path}.imagePolicy.reason", max_length=300)
    else:
        raise ValidationError([f"{path}.imagePolicy.mode: expected required or notApplicable"])
    return value


def _validate_image_policy(rounds: list[dict[str, Any]], session: dict[str, Any]) -> None:
    mode = session["imagePolicy"]["mode"]
    errors: list[str] = []
    for round_value in rounds:
        for option_index, option in enumerate(round_value["options"]):
            has_images = "image" in option or "images" in option
            path = f"round {round_value['number']}.options[{option_index}]"
            if mode == "required" and not has_images:
                errors.append(f"{path}: requires at least one image because session.imagePolicy.mode is required")
            elif mode == "notApplicable" and has_images:
                errors.append(f"{path}: images are not allowed because session.imagePolicy.mode is notApplicable")
    if errors:
        raise ValidationError(errors)


def validate_seed(seed: Any) -> dict[str, Any]:
    root = _object(seed, "seed", {"protocol", "schemaVersion", "runtimeVersion", "session", "profileExclusions", "history", "round"})
    if root.get("protocol") != PROTOCOL:
        raise ValidationError(["seed.protocol: unsupported protocol"])
    if root.get("schemaVersion") != SCHEMA_VERSION:
        raise ValidationError([f"seed.schemaVersion: expected {SCHEMA_VERSION}"])
    if root.get("runtimeVersion") != RUNTIME_VERSION:
        raise ValidationError([f"seed.runtimeVersion: expected {RUNTIME_VERSION}"])
    session = _validate_session(_required(root, "session", "seed"))
    _validate_profile_exclusions(_required(root, "profileExclusions", "seed"), "seed.profileExclusions")
    history_raw = _array(_required(root, "history", "seed"), "seed.history")
    history = [_validate_round(value, index + 1, True, f"seed.history[{index}]") for index, value in enumerate(history_raw)]
    current = _validate_round(_required(root, "round", "seed"), len(history) + 1, False, "seed.round")
    _validate_round_lineage([*history, current], session)
    _validate_image_policy([*history, current], session)
    return root


def validate_continuation(value: Any) -> dict[str, Any]:
    root = _object(value, "continuation", {"protocol", "schemaVersion", "parent", "session", "parentProfileExclusions", "profileExclusions", "completedRounds", "nextRoundNumber"})
    if root.get("protocol") != CONTINUATION_PROTOCOL:
        raise ValidationError(["continuation.protocol: unsupported protocol"])
    if root.get("schemaVersion") != SCHEMA_VERSION:
        raise ValidationError([f"continuation.schemaVersion: expected {SCHEMA_VERSION}"])
    parent = _object(_required(root, "parent", "continuation"), "continuation.parent", {"sessionId", "roundNumber", "seedHash", "url"})
    _id(_required(parent, "sessionId", "continuation.parent"), "continuation.parent.sessionId")
    if not isinstance(parent.get("roundNumber"), int) or isinstance(parent.get("roundNumber"), bool) or parent["roundNumber"] < 1:
        raise ValidationError(["continuation.parent.roundNumber: expected a positive integer"])
    if not isinstance(parent.get("seedHash"), str) or not HASH_RE.fullmatch(parent["seedHash"]):
        raise ValidationError(["continuation.parent.seedHash: expected a SHA-256 hash"])
    _https(_required(parent, "url", "continuation.parent"), "continuation.parent.url")
    session = _validate_session(_required(root, "session", "continuation"), "continuation.session")
    parent_profile_exclusions = _validate_profile_exclusions(_required(root, "parentProfileExclusions", "continuation"), "continuation.parentProfileExclusions")
    _validate_profile_exclusions(_required(root, "profileExclusions", "continuation"), "continuation.profileExclusions")
    completed_raw = _array(_required(root, "completedRounds", "continuation"), "continuation.completedRounds")
    if len(completed_raw) != parent["roundNumber"]:
        raise ValidationError(["continuation.completedRounds: does not match parent round number"])
    completed = [_validate_round(value, index + 1, True, f"continuation.completedRounds[{index}]") for index, value in enumerate(completed_raw)]
    next_number = root.get("nextRoundNumber")
    if not isinstance(next_number, int) or isinstance(next_number, bool) or next_number != parent["roundNumber"] + 1:
        raise ValidationError(["continuation.nextRoundNumber: expected parent round number + 1"])
    if parent["sessionId"] != session["id"]:
        raise ValidationError(["continuation.parent.sessionId: does not match continuation.session.id"])
    _validate_round_lineage(completed, session)
    _validate_image_policy(completed, session)
    parent_round = {key: value for key, value in completed[-1].items() if key != "verdicts"}
    parent_seed = {"protocol": PROTOCOL, "schemaVersion": SCHEMA_VERSION, "runtimeVersion": RUNTIME_VERSION, "session": session, "profileExclusions": parent_profile_exclusions, "history": completed[:-1], "round": parent_round}
    if seed_hash(parent_seed) != parent["seedHash"]:
        raise ValidationError(["continuation.parent.seedHash: does not match the completed parent seed"])
    return root


def validate_successor(continuation: Any, next_seed: Any) -> dict[str, Any]:
    continuation = validate_continuation(continuation)
    next_seed = validate_seed(next_seed)
    if canonical_json(next_seed["session"]) != canonical_json(continuation["session"]):
        raise ValidationError(["successor.session: immutable session fields changed"])
    if canonical_json(next_seed["history"]) != canonical_json(continuation["completedRounds"]):
        raise ValidationError(["successor.history: completed rounds changed or are missing"])
    if canonical_json(next_seed["profileExclusions"]) != canonical_json(continuation["profileExclusions"]):
        raise ValidationError(["successor.profileExclusions: current profile exclusions changed or are missing"])
    if next_seed["round"]["number"] != continuation["nextRoundNumber"]:
        raise ValidationError(["successor.round.number: incorrect next round number"])
    return next_seed


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


def _read_asset(path: Path, description: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublishError(f"{description} is unavailable: {exc}") from exc


def _font_data() -> str:
    path = Path(__file__).resolve().parents[1] / "assets" / "fonts" / "SpaceGrotesk-latin.woff2"
    try:
        return "data:font/woff2;base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    except OSError as exc:
        raise PublishError(f"Space Grotesk font is unavailable: {exc}") from exc


def build_html(seed: dict[str, Any], *, expires_at: str | None = None, template_path: Path | None = None) -> bytes:
    validate_seed(seed)
    root = Path(__file__).resolve().parents[1]
    template = template_path or root / "assets" / "runtime.html"
    html = _read_asset(template, "runtime template")
    css = _read_asset(root / "assets" / "runtime.css", "runtime CSS")
    core = _read_asset(root / "assets" / "runtime-core.js", "runtime core")
    ui = _read_asset(root / "assets" / "runtime-ui.js", "runtime UI")
    icons_dir = root / "assets" / "icons"
    icons: dict[str, str] = {}
    try:
        for icon_path in sorted(icons_dir.glob("*.svg")):
            icons[icon_path.stem] = icon_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise PublishError(f"runtime icons are unavailable: {exc}") from exc
    digest = seed_hash(seed)
    encoded = base64.b64encode(canonical_json(seed)).decode("ascii")
    css = css.replace(FONT_TOKEN, _font_data())
    ui = ui.replace(ICONS_TOKEN, json.dumps(icons, ensure_ascii=False, separators=(",", ":")))
    ui = ui.replace(SEED_TOKEN, encoded).replace(HASH_TOKEN, digest)
    replacements = {
        HASH_TOKEN: digest,
        SESSION_TOKEN: seed["session"]["id"],
        RUNTIME_TOKEN: RUNTIME_VERSION,
        EXPIRATION_PLACEHOLDER: _utc_expiration(expires_at),
        CSS_TOKEN: css,
        CORE_TOKEN: core,
        UI_TOKEN: ui,
    }
    for token, replacement in replacements.items():
        if token not in html:
            raise PublishError(f"runtime template is missing placeholder {token}")
        html = html.replace(token, replacement)
    remaining = (SEED_TOKEN, HASH_TOKEN, SESSION_TOKEN, RUNTIME_TOKEN, CSS_TOKEN, CORE_TOKEN, UI_TOKEN, ICONS_TOKEN, FONT_TOKEN)
    if any(token in html for token in remaining):
        raise PublishError("runtime template placeholders were not fully compiled")
    return html.encode("utf-8")


def _current_image_entries(seed: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    for option_index, option in enumerate(seed["round"]["options"]):
        if "images" in option:
            images = option["images"]
            image_path = f"seed.round.options[{option_index}].images"
        elif "image" in option:
            images = [option["image"]]
            image_path = f"seed.round.options[{option_index}].image"
        else:
            continue
        for image_index, image in enumerate(images):
            suffix = f"[{image_index}]" if "images" in option else ""
            yield f"{image_path}{suffix}", image


def _current_image_manifest(seed: dict[str, Any]) -> list[dict[str, str]]:
    return [{"path": path, "url": image["url"]} for path, image in _current_image_entries(seed)]


def _unique_manifest_urls(manifest: list[dict[str, str]]) -> list[str]:
    return list(dict.fromkeys(item["url"] for item in manifest))


def _image_type(body: bytes) -> str | None:
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if body.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(body) >= 12 and body[:4] == b"RIFF" and body[8:12] == b"WEBP":
        return "image/webp"
    if len(body) >= 12 and body[4:8] == b"ftyp" and body[8:12] in {b"avif", b"avis"}:
        return "image/avif"
    return None


def _fetch_image(url: str, *, timeout: float = 15) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": ", ".join(sorted(IMAGE_CONTENT_TYPES)), "User-Agent": "winnow-image-verifier/2"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            if status < 200 or status >= 300:
                raise ValueError(f"HTTP status {status}")
            final_url = response.geturl() if hasattr(response, "geturl") else url
            final_parsed = _https(final_url, "image.finalUrl")
            headers = getattr(response, "headers", {})
            content_type = headers.get_content_type() if hasattr(headers, "get_content_type") else str(headers.get("Content-Type", "")).split(";", 1)[0].strip().lower()
            content_type = "image/jpeg" if content_type == "image/jpg" else content_type.lower()
            if content_type not in IMAGE_CONTENT_TYPES:
                raise ValueError(f"unsupported content type {content_type or 'missing'}")
            content_length = headers.get("Content-Length")
            if content_length is not None:
                try:
                    if int(content_length) > IMAGE_MAX_BYTES:
                        raise ValueError(f"response exceeds {IMAGE_MAX_BYTES} bytes")
                except ValueError as exc:
                    if str(exc).startswith("response exceeds"):
                        raise
                    raise ValueError("invalid Content-Length") from exc
            body = response.read(IMAGE_MAX_BYTES + 1)
            if len(body) > IMAGE_MAX_BYTES:
                raise ValueError(f"response exceeds {IMAGE_MAX_BYTES} bytes")
            detected_type = _image_type(body)
            if detected_type is None:
                raise ValueError("image signature was not recognized")
            if detected_type != content_type:
                raise ValueError(f"content type {content_type} does not match {detected_type} bytes")
            return {
                "url": final_parsed.geturl(),
                "contentType": content_type,
                "bytes": len(body),
            }
    except urllib.error.HTTPError as exc:
        raise ValueError(f"HTTP status {exc.code}") from None
    except (urllib.error.URLError, TimeoutError, UnicodeError) as exc:
        raise ValueError(f"network error ({type(exc).__name__})") from None


def _verify_current_image_urls(manifest: list[dict[str, str]], *, timeout: float) -> list[dict[str, Any]]:
    references: dict[str, list[str]] = {}
    for item in manifest:
        references.setdefault(item["url"], []).append(item["path"])
    unique_urls = list(references)
    if not unique_urls:
        return []
    errors: list[tuple[int, str]] = []
    verified: list[dict[str, Any] | None] = [None] * len(unique_urls)

    with concurrent.futures.ThreadPoolExecutor(max_workers=min(4, len(unique_urls))) as executor:
        futures = {executor.submit(_fetch_image, url, timeout=timeout): index for index, url in enumerate(unique_urls)}
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            url = unique_urls[index]
            paths = references[url]
            try:
                result = future.result()
                if not isinstance(result, dict):
                    raise ValueError("invalid image verification result")
                verified[index] = dict(result)
            except (ValueError, ValidationError) as exc:
                errors.append((index, f"{', '.join(paths)}: {exc}"))
    if errors:
        errors.sort(key=lambda error: error[0])
        raise ValidationError([f"image verification failed: {message}" for _index, message in errors])
    return [result for result in verified if result is not None]


def verify_image_urls(
    seed: Any,
    *,
    timeout: float = 15,
) -> dict[str, Any]:
    seed = validate_seed(seed)
    current_manifest = _current_image_manifest(seed)
    image_count = len(current_manifest)
    unique_image_count = len(_unique_manifest_urls(current_manifest))
    result_base = {
        "scope": "currentRound",
        "images": image_count,
        "uniqueImages": unique_image_count,
    }
    if not current_manifest:
        return {**result_base, "verified": []}
    verified = _verify_current_image_urls(current_manifest, timeout=timeout)
    return {**result_base, "verified": verified}


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
            return response.status, json.loads(raw.decode("utf-8")) if raw else {}
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
        raise PublishError(f"here.now upload failed ({getattr(exc, 'code', 'network')})") from None


def _meta_tag(name: str, value: str) -> str:
    return f'<meta name="{name}" content="{value}">'


def _fetch_live(
    url: str,
    expected_session_id: str,
    expected_seed_hash: str,
    expected_runtime_version: str,
    expected_expires_at: str,
    *,
    allow_http: bool = False,
    timeout: float = 30,
) -> None:
    parsed = urllib.parse.urlparse(url)
    if parsed.scheme != "https" and not allow_http:
        raise PublishError("here.now returned a non-HTTPS site URL")
    request = urllib.request.Request(url, headers={"Accept": "text/html"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 200) or 200)
            if status < 200 or status >= 300:
                raise PublishError(f"live Site verification failed (HTTP {status})")
            html = response.read(2_000_000).decode("utf-8", errors="strict")
    except PublishError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, UnicodeDecodeError) as exc:
        raise PublishError(f"live Site verification failed ({getattr(exc, 'code', 'network')})") from None
    expected_markers = {
        "session id": _meta_tag("winnow-session-id", expected_session_id),
        "seed hash": _meta_tag("winnow-seed-hash", expected_seed_hash),
        "runtime version": _meta_tag("winnow-runtime-version", expected_runtime_version),
        "expiration": _meta_tag("winnow-expires-at", expected_expires_at),
    }
    for label, marker in expected_markers.items():
        if marker not in html:
            raise PublishError(f"live Site verification failed: {label} mismatch")


def _elapsed_ms(started: float, finished: float | None = None) -> int:
    end = time.monotonic() if finished is None else finished
    return max(0, int(round((end - started) * 1000)))


def publish(
    seed: dict[str, Any],
    *,
    continuation: dict[str, Any] | None = None,
    endpoint: str = PUBLISH_ENDPOINT,
    allow_http_test: bool = False,
) -> dict[str, Any]:
    total_started = time.monotonic()
    validation_started = total_started
    validate_seed(seed)
    if seed["history"]:
        if continuation is None:
            raise ValidationError(["publish: a continuation is required for rounds after Round 1"])
        validate_successor(continuation, seed)
    elif continuation is not None:
        validate_successor(continuation, seed)
    digest = seed_hash(seed)
    validation_ms = _elapsed_ms(validation_started)
    image_started = time.monotonic()
    image_verification = verify_image_urls(seed)
    image_verification_ms = _elapsed_ms(image_started)
    publication_started = time.monotonic()
    html = build_html(seed)
    status, created = _http_json(endpoint, "POST", {"files": [{"path": "index.html", "size": len(html), "contentType": CONTENT_TYPE}]}, headers={"X-HereNow-Client": "winnow-portable/2"})
    if status < 200 or status >= 300 or created.get("anonymous") is not True:
        raise PublishError("here.now did not create an anonymous Site")
    expires_at = created.get("expiresAt")
    if not isinstance(expires_at, str):
        raise PublishError("here.now anonymous response is missing expiresAt")
    upload = _object(created.get("upload"), "publish.upload")
    uploads = _array(upload.get("uploads"), "publish.upload.uploads")
    matching = next((item for item in uploads if isinstance(item, dict) and item.get("path") == "index.html"), None)
    if matching is None or not isinstance(matching.get("url"), str):
        raise PublishError("here.now did not return an index.html upload URL")
    html = build_html(seed, expires_at=expires_at)
    _http_upload(matching["url"], html, matching.get("headers"))
    finalize_url = upload.get("finalizeUrl")
    version_id = upload.get("versionId")
    if not isinstance(finalize_url, str) or not isinstance(version_id, str):
        raise PublishError("here.now create response is missing finalize metadata")
    _http_json(finalize_url, "POST", {"versionId": version_id})
    site_url = created.get("siteUrl")
    if not isinstance(site_url, str):
        raise PublishError("here.now create response is missing siteUrl")
    _fetch_live(
        site_url,
        seed["session"]["id"],
        digest,
        RUNTIME_VERSION,
        _utc_expiration(expires_at),
        allow_http=allow_http_test,
    )
    site_publication_ms = _elapsed_ms(publication_started)
    total_ms = _elapsed_ms(total_started)
    return {
        "siteUrl": site_url,
        "expiresAt": expires_at,
        "sessionId": seed["session"]["id"],
        "seedHash": digest,
        "roundNumber": seed["round"]["number"],
        "imageVerification": {
            "scope": image_verification.get("scope", "currentRound"),
            "images": image_verification.get("images", 0),
            "uniqueImages": image_verification.get("uniqueImages", 0),
        },
        "timingsMs": {
            "validation": validation_ms,
            "imageVerification": image_verification_ms,
            "sitePublication": site_publication_ms,
            "total": total_ms,
        },
    }


def inspect_continuation(value: dict[str, Any]) -> dict[str, Any]:
    validate_continuation(value)
    return {"parentSessionId": value["parent"]["sessionId"], "roundNumber": value["parent"]["roundNumber"], "completedRounds": len(value["completedRounds"]), "nextRoundNumber": value["nextRoundNumber"]}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Portable Winnow v3 compiler and anonymous here.now publisher")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate_parser = subparsers.add_parser("validate", help="validate a seed JSON file")
    validate_parser.add_argument("seed", type=Path)
    verify_images_parser = subparsers.add_parser("verify-images", help="fetch and verify current-round image URLs")
    verify_images_parser.add_argument("seed", type=Path)
    verify_images_parser.add_argument("--timeout", type=float, default=15, help="per-image network timeout in seconds")
    inspect_parser = subparsers.add_parser("inspect-continuation", help="validate and summarize a continuation package")
    inspect_parser.add_argument("continuation", type=Path)
    successor_parser = subparsers.add_parser("validate-successor", help="validate a successor seed against a continuation")
    successor_parser.add_argument("continuation", type=Path)
    successor_parser.add_argument("next_seed", type=Path)
    publish_parser = subparsers.add_parser("publish", help="create and publish a new anonymous here.now Site")
    publish_parser.add_argument("seed", type=Path)
    publish_parser.add_argument("--continuation", type=Path)
    publish_parser.add_argument("--endpoint", default=PUBLISH_ENDPOINT, help=argparse.SUPPRESS)
    publish_parser.add_argument("--allow-http-test", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.command == "validate":
            seed = read_json(args.seed)
            validate_seed(seed)
            print(json.dumps({"valid": True, "seedHash": seed_hash(seed)}, separators=(",", ":")))
        elif args.command == "verify-images":
            result = verify_image_urls(read_json(args.seed), timeout=args.timeout)
            print(json.dumps({"valid": True, **result}, separators=(",", ":")))
        elif args.command == "inspect-continuation":
            print(json.dumps(inspect_continuation(read_json(args.continuation)), separators=(",", ":")))
        elif args.command == "validate-successor":
            validate_successor(read_json(args.continuation), read_json(args.next_seed))
            print(json.dumps({"valid": True}, separators=(",", ":")))
        else:
            continuation = read_json(args.continuation) if args.continuation else None
            result = publish(read_json(args.seed), continuation=continuation, endpoint=args.endpoint, allow_http_test=args.allow_http_test)
            print(json.dumps(result, separators=(",", ":")))
        return 0
    except (ValidationError, PublishError, OSError) as exc:
        print(f"winnow: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
