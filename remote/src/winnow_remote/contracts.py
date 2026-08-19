"""Closed, transport-independent contracts for Winnow Remote.

These parsers deliberately live below HTTP/MCP.  A transport must pass decoded
JSON through them before it is allowed to call the coordinator, which keeps the
browser boundary from gradually acquiring a second continuation format.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Mapping

from .herenow import (
    MAX_REMOTE_BROWSER_REQUEST_BYTES,
    MAX_REMOTE_CREATE_SEED_BYTES,
    MAX_REMOTE_MCP_RESULT_BYTES,
    MAX_REMOTE_MCP_REQUEST_BYTES,
    MAX_REMOTE_SUCCESSOR_SEED_BYTES,
)


BROWSER_REQUEST_PROTOCOL = "winnow.browser-request"
BROWSER_REQUEST_VERSION = 1
HASH_RE = re.compile(r"^[0-9a-f]{64}$")
MAX_PROFILE_SELECTIONS = 6
MAX_BROWSER_VERDICTS = 10


class ContractError(ValueError):
    """A bounded, path-oriented contract error safe for a transport response."""


def canonical_json(value: Any) -> bytes:
    """Return the one JSON representation used for digests and byte limits."""

    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ContractError("value cannot be serialized as canonical JSON") from exc


def bounded_json(value: Any, *, limit: int, path: str) -> bytes:
    encoded = canonical_json(value)
    if len(encoded) > limit:
        raise ContractError(f"{path}: exceeds the byte limit")
    return encoded


def digest_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


def _object(value: Any, path: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, dict):
        raise ContractError(f"{path}: expected an object")
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise ContractError(f"{path}: unknown properties: {', '.join(unknown)}")
    return value


def _required(value: Mapping[str, Any], key: str, path: str) -> Any:
    if key not in value:
        raise ContractError(f"{path}.{key}: is required")
    return value[key]


def _positive_int(value: Any, path: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ContractError(f"{path}: expected a positive integer")
    return value


def _hash(value: Any, path: str) -> str:
    if not isinstance(value, str) or not HASH_RE.fullmatch(value):
        raise ContractError(f"{path}: expected a SHA-256 hash")
    return value


def _token(value: Any, path: str, *, maximum: int = 512) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or value != value.strip():
        raise ContractError(f"{path}: expected bounded non-empty text")
    if any(ord(character) < 33 or ord(character) == 127 for character in value):
        raise ContractError(f"{path}: contains invalid characters")
    return value


def _uuid(value: Any, path: str) -> str:
    if not isinstance(value, str):
        raise ContractError(f"{path}: expected a UUID")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ContractError(f"{path}: expected a UUID") from exc
    if str(parsed) != value:
        raise ContractError(f"{path}: expected a canonical UUID")
    return value.lower()


@dataclass(frozen=True)
class BrowserVerdict:
    option_id: str
    decision: str

    @classmethod
    def parse(cls, value: Any, *, path: str) -> "BrowserVerdict":
        raw = _object(value, path, {"optionId", "decision"})
        option_id = _token(_required(raw, "optionId", path), f"{path}.optionId", maximum=128)
        decision = _required(raw, "decision", path)
        if decision not in {"like", "dislike", "skip"}:
            raise ContractError(f"{path}.decision: expected like, dislike, or skip")
        return cls(option_id=option_id, decision=decision)

    def as_dict(self) -> dict[str, str]:
        return {"optionId": self.option_id, "decision": self.decision}


@dataclass(frozen=True)
class BrowserNextRoundRequest:
    idempotency_key: str
    round_number: int
    seed_hash: str
    published_revision: int
    verdicts: tuple[BrowserVerdict, ...]
    selected_profile_keys: tuple[str, ...]
    digest: str

    @classmethod
    def parse(cls, value: Any) -> "BrowserNextRoundRequest":
        encoded = bounded_json(value, limit=MAX_REMOTE_BROWSER_REQUEST_BYTES, path="browser request")
        raw = _object(
            value,
            "browser request",
            {"protocol", "version", "idempotencyKey", "roundNumber", "seedHash", "publishedRevision", "verdicts", "selectedProfileKeys"},
        )
        if raw.get("protocol") != BROWSER_REQUEST_PROTOCOL:
            raise ContractError("browser request.protocol: unsupported protocol")
        if raw.get("version") != BROWSER_REQUEST_VERSION:
            raise ContractError("browser request.version: expected 1")
        idempotency_key = _uuid(_required(raw, "idempotencyKey", "browser request"), "browser request.idempotencyKey")
        round_number = _positive_int(_required(raw, "roundNumber", "browser request"), "browser request.roundNumber")
        seed_hash = _hash(_required(raw, "seedHash", "browser request"), "browser request.seedHash")
        published_revision = _positive_int(_required(raw, "publishedRevision", "browser request"), "browser request.publishedRevision")
        verdicts_raw = _required(raw, "verdicts", "browser request")
        if not isinstance(verdicts_raw, list) or not verdicts_raw or len(verdicts_raw) > MAX_BROWSER_VERDICTS:
            raise ContractError("browser request.verdicts: expected 1–10 verdicts")
        verdicts = tuple(BrowserVerdict.parse(item, path=f"browser request.verdicts[{index}]") for index, item in enumerate(verdicts_raw))
        selected_raw = _required(raw, "selectedProfileKeys", "browser request")
        if not isinstance(selected_raw, list) or len(selected_raw) > MAX_PROFILE_SELECTIONS:
            raise ContractError("browser request.selectedProfileKeys: allows at most six keys")
        selected = tuple(_token(item, f"browser request.selectedProfileKeys[{index}]", maximum=500) for index, item in enumerate(selected_raw))
        if len(set(selected)) != len(selected):
            raise ContractError("browser request.selectedProfileKeys: duplicate keys are not allowed")
        return cls(
            idempotency_key=idempotency_key,
            round_number=round_number,
            seed_hash=seed_hash,
            published_revision=published_revision,
            verdicts=verdicts,
            selected_profile_keys=selected,
            digest=hashlib.sha256(encoded).hexdigest(),
        )


@dataclass(frozen=True)
class CreateWinnowSessionRequest:
    """The exact public MCP creation contract."""

    seed: Mapping[str, Any]

    @classmethod
    def parse(cls, value: Any) -> "CreateWinnowSessionRequest":
        bounded_json(value, limit=MAX_REMOTE_MCP_REQUEST_BYTES, path="create request")
        raw = _object(value, "create request", {"seed", "mode"})
        if raw.get("mode") != "rolling":
            raise ContractError("create request.mode: expected rolling")
        seed = _required(raw, "seed", "create request")
        if not isinstance(seed, dict):
            raise ContractError("create request.seed: expected an object")
        bounded_json(seed, limit=MAX_REMOTE_CREATE_SEED_BYTES, path="create request.seed")
        return cls(seed=seed)


@dataclass(frozen=True)
class WaitForContinueRequest:
    expected_round_number: int
    expected_seed_hash: str
    max_wait_seconds: int

    @classmethod
    def parse(cls, value: Any, *, maximum_wait_seconds: int) -> "WaitForContinueRequest":
        bounded_json(value, limit=MAX_REMOTE_MCP_RESULT_BYTES, path="wait request")
        raw = _object(value, "wait request", {"sessionHandle", "expectedRoundNumber", "expectedSeedHash", "maxWaitSeconds"})
        _token(_required(raw, "sessionHandle", "wait request"), "wait request.sessionHandle")
        requested = _positive_int(_required(raw, "maxWaitSeconds", "wait request"), "wait request.maxWaitSeconds")
        return cls(
            expected_round_number=_positive_int(_required(raw, "expectedRoundNumber", "wait request"), "wait request.expectedRoundNumber"),
            expected_seed_hash=_hash(_required(raw, "expectedSeedHash", "wait request"), "wait request.expectedSeedHash"),
            max_wait_seconds=min(requested, maximum_wait_seconds),
        )


@dataclass(frozen=True)
class PublishNextRoundRequest:
    event_id: str
    publish_fence: str
    parent_seed_hash: str
    next_seed: Mapping[str, Any]
    next_seed_hash: str

    @classmethod
    def parse(cls, value: Any) -> "PublishNextRoundRequest":
        bounded_json(value, limit=MAX_REMOTE_MCP_REQUEST_BYTES, path="publish request")
        raw = _object(value, "publish request", {"sessionHandle", "eventId", "publishFence", "parentSeedHash", "nextSeed"})
        _token(_required(raw, "sessionHandle", "publish request"), "publish request.sessionHandle")
        seed = _required(raw, "nextSeed", "publish request")
        bounded_json(seed, limit=MAX_REMOTE_SUCCESSOR_SEED_BYTES, path="publish request.nextSeed")
        if not isinstance(seed, dict):
            raise ContractError("publish request.nextSeed: expected an object")
        return cls(
            event_id=_token(_required(raw, "eventId", "publish request"), "publish request.eventId"),
            publish_fence=_token(_required(raw, "publishFence", "publish request"), "publish request.publishFence"),
            parent_seed_hash=_hash(_required(raw, "parentSeedHash", "publish request"), "publish request.parentSeedHash"),
            next_seed=seed,
            next_seed_hash=digest_json(seed),
        )
