"""Transport-independent Winnow Remote coordinator.

HTTP/MCP adapters must not encode state transitions themselves.  This module
owns all capability checks, bounded browser reconstruction, and CAS-based
lifecycle changes so a dropped connection or competing caller cannot invent a
second event or overwrite a published revision.
"""

from __future__ import annotations

import copy
import datetime as dt
import hashlib
import hmac
import importlib.util
import ipaddress
import json
import math
import secrets
import sys
import unicodedata
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from .contracts import (
    BrowserNextRoundRequest,
    ContractError,
    PublishNextRoundRequest,
    WaitForContinueRequest,
    canonical_json,
)
from .herenow import (
    MAX_REMOTE_CONTINUATION_HANDOFF_BYTES,
    MAX_REMOTE_CREATE_SEED_BYTES,
    MAX_REMOTE_MCP_RESULT_BYTES,
    MAX_REMOTE_STORED_RECORD_BYTES,
    MAX_REMOTE_SUCCESSOR_SEED_BYTES,
)
from .repository import ActiveSession, RecordConflict, StoredSession, TerminalTombstone, TransactionalRepository
from .security import CapabilitySecurity, EncryptedSecret, SecretError


MAX_SESSION_OPTIONS = 100
DEFAULT_DAILY_QUOTA = 10
_CIRCUIT_MODES = frozenset({"normal", "no_new_sessions", "read_only_existing", "status_only"})
_TERMINAL_STATUSES = frozenset({"complete", "expired", "failed", "circuit_open", "disconnected"})


class CoordinatorError(RuntimeError):
    """A bounded domain error suitable for an adapter's safe error mapping."""


class AuthenticationError(CoordinatorError):
    pass


class StateConflict(CoordinatorError):
    pass


class CircuitOpen(CoordinatorError):
    pass


class QuotaExceeded(CoordinatorError):
    pass


def _load_portable_core() -> Any:
    existing = sys.modules.get("winnow_portable_core")
    if existing is not None:
        return existing
    root = Path(__file__).resolve().parents[3]
    path = root / ".agents" / "skills" / "winnow" / "scripts" / "winnow.py"
    spec = importlib.util.spec_from_file_location("winnow_portable_core", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("portable Winnow validator is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _to_timestamp(value: dt.datetime) -> float:
    return value.astimezone(dt.timezone.utc).timestamp()


def _to_iso(value: float | None) -> str | None:
    if value is None:
        return None
    return dt.datetime.fromtimestamp(value, dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _parse_iso_timestamp(value: str) -> float:
    if not isinstance(value, str):
        raise CoordinatorError("expiration is invalid")
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CoordinatorError("expiration is invalid") from exc
    if parsed.tzinfo is None:
        raise CoordinatorError("expiration is invalid")
    return parsed.timestamp()


def _clone(value: Any) -> Any:
    return copy.deepcopy(value)


def normalize_public_origin(site_url: str, *, allowed_host_suffixes: tuple[str, ...]) -> tuple[str, str]:
    """Return the public URL and exact CORS origin for a single-slug host.

    HereNow's public URL must be HTTPS, have no credentials/query/fragment and
    terminate at one host-slug's root.  The result intentionally excludes a
    path, so terminal tombstones retain no public URL path.
    """

    if not isinstance(site_url, str) or len(site_url) > 2048:
        raise CoordinatorError("public site URL is invalid")
    try:
        parsed = urllib.parse.urlsplit(site_url)
    except ValueError as exc:
        raise CoordinatorError("public site URL is invalid") from exc
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CoordinatorError("public site URL is invalid")
    suffix = next((item.lower() for item in allowed_host_suffixes if host.endswith(item.lower())), None)
    if suffix is None or host == suffix.lstrip("."):
        raise CoordinatorError("public site URL is not an allowed HereNow origin")
    slug = host[: -len(suffix)] if suffix else ""
    if not slug or "." in slug or not all(character.isalnum() or character == "-" for character in slug):
        raise CoordinatorError("public site URL is not a single-slug host")
    origin = f"https://{host}"
    return origin + "/", origin


def canonical_preflight_origin(origin: str, *, allowed_host_suffixes: tuple[str, ...]) -> bool:
    """Validate a no-capability preflight origin syntactically only."""

    try:
        _url, normalized = normalize_public_origin(origin + "/", allowed_host_suffixes=allowed_host_suffixes)
    except CoordinatorError:
        return False
    return hmac.compare_digest(origin, normalized)


def normalize_network_prefix(source_address: str) -> str:
    """Normalize a transport-proven source to the v1 quota network bucket.

    The caller is responsible for deciding whether a proxy header is trusted;
    this helper never examines headers.  It accepts only one address and emits
    IPv4 /24 or IPv6 /64, so raw addresses never become durable quota keys.
    """

    if not isinstance(source_address, str) or not source_address or len(source_address) > 128:
        raise CoordinatorError("source address is invalid")
    try:
        address = ipaddress.ip_address(source_address)
    except ValueError as exc:
        raise CoordinatorError("source address is invalid") from exc
    prefix = 24 if isinstance(address, ipaddress.IPv4Address) else 64
    return str(ipaddress.ip_network(f"{address}/{prefix}", strict=False))


def _normalized_pattern_value(value: Any) -> str | bool | None:
    if isinstance(value, str):
        return " ".join(unicodedata.normalize("NFKC", value).strip().split()).lower()
    if isinstance(value, bool):
        return value
    return None


def _profile_pattern_key(factor_id: str, polarity: str, direction: str, value: Any) -> str:
    return canonical_json({"factorId": factor_id, "polarity": polarity, "direction": direction, "value": _normalized_pattern_value(value)}).decode("utf-8")


def _value_map(option: Mapping[str, Any]) -> dict[str, Any]:
    return {str(item["factorId"]): item.get("value") for item in option.get("values", []) if isinstance(item, Mapping) and "factorId" in item}


def _pattern_record(factor: Mapping[str, Any], polarity: str, support: int, strength: float, direction: str, value: Any, mean: Any) -> dict[str, Any]:
    return {
        "key": _profile_pattern_key(str(factor["id"]), polarity, direction, value),
        "factorId": factor["id"],
        "polarity": polarity,
        "direction": direction,
        "value": value,
        "mean": _javascript_number(mean),
        "supportCount": support,
        "strength": _javascript_number(strength),
    }


def _javascript_number(value: Any) -> Any:
    """Match JSON.stringify's single JavaScript Number representation.

    Python's ``1.0`` is semantically equal to JavaScript's ``1`` but hashes to
    different canonical JSON bytes.  Pattern values participate in successor
    equality, so integral finite floats must serialize as integers here.
    """

    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    return value


def _label_noun(value: str) -> str:
    lowered = value.lower()
    if lowered == "price":
        return "prices"
    return lowered if lowered.endswith("s") else lowered


def _profile_candidates(seed: Mapping[str, Any], verdicts: Mapping[str, str]) -> list[dict[str, Any]]:
    """Python implementation pinned to runtime-core.js v4's candidate rules.

    It returns record-only candidates—no UI labels or descriptions can cross the
    browser boundary.  Tests compare its continuation with the JS runtime for
    representative numeric, category, and boolean inputs.
    """

    rows_by_factor: dict[str, list[tuple[Mapping[str, Any], Any, str]]] = {}
    rounds = [*seed.get("history", []), seed.get("round")]
    for round_value in rounds:
        if not isinstance(round_value, Mapping):
            continue
        factors = {str(item["id"]): item for item in round_value.get("factors", []) if isinstance(item, Mapping) and "id" in item}
        decisions = {str(item["optionId"]): item.get("decision") for item in round_value.get("verdicts", []) if isinstance(item, Mapping)}
        if round_value is seed.get("round"):
            decisions.update(verdicts)
        for option in round_value.get("options", []):
            if not isinstance(option, Mapping):
                continue
            decision = decisions.get(str(option.get("id")))
            if decision not in {"like", "dislike"}:
                continue
            for factor_id, value in _value_map(option).items():
                factor = factors.get(factor_id)
                if factor is None or value is None or value == "unknown":
                    continue
                rows_by_factor.setdefault(factor_id, []).append((factor, value, decision))
    current_order = {str(item["id"]): index for index, item in enumerate(seed.get("round", {}).get("factors", [])) if isinstance(item, Mapping) and "id" in item}
    candidates: list[dict[str, Any]] = []
    for factor_id, rows in rows_by_factor.items():
        factor = rows[0][0]
        value_type = factor.get("valueType")
        if value_type == "boolean":
            votes = {True: 0, False: 0}
            for _factor, value, decision in rows:
                if isinstance(value, bool):
                    votes[value if decision == "like" else not value] += 1
            total = votes[True] + votes[False]
            preferred = True if votes[True] > votes[False] else False if votes[False] > votes[True] else None
            if preferred is not None and total:
                support = votes[preferred]
                strength = support / total
                if support >= 2 and strength >= 2 / 3:
                    candidates.append(_pattern_record(factor, "like", support, strength, "include" if preferred else "exclude", preferred, None))
            continue
        for polarity in ("like", "dislike"):
            same = [value for _factor, value, decision in rows if decision == polarity]
            other = [value for _factor, value, decision in rows if decision != polarity]
            if value_type == "number":
                values = [value for value in same if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)]
                opposite = [value for value in other if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)]
                if len(values) < 2:
                    continue
                mean = sum(values) / len(values)
                all_values = [*values, *opposite]
                minimum, maximum = min(all_values), max(all_values)
                if len(opposite) >= 2 and maximum != minimum:
                    opposite_mean = sum(opposite) / len(opposite)
                    difference = abs(mean - opposite_mean) / (maximum - minimum)
                    if difference >= 0.20:
                        candidates.append(_pattern_record(factor, polarity, len(values), difference, "lower" if mean < opposite_mean else "higher", None, None))
                        continue
                candidates.append(_pattern_record(factor, polarity, len(values), 1, "average", None, mean))
                continue
            if value_type != "category":
                continue
            grouped: dict[str, tuple[Any, int]] = {}
            for value in same:
                if not isinstance(value, str):
                    continue
                key = json.dumps(_normalized_pattern_value(value), ensure_ascii=False, separators=(",", ":"))
                original, count = grouped.get(key, (value, 0))
                grouped[key] = (original, count + 1)
            for key, (value, support) in grouped.items():
                if support < 2:
                    continue
                opposite_matches = sum(1 for other_value in other if json.dumps(_normalized_pattern_value(other_value), ensure_ascii=False, separators=(",", ":")) == key)
                difference = abs(support / len(same) - (opposite_matches / len(other) if other else 0))
                if other and difference < 0.25:
                    continue
                candidates.append(_pattern_record(factor, polarity, support, difference or 1, "include", value, None))
    indexed = list(enumerate(candidates))
    indexed.sort(key=lambda item: (-item[1]["supportCount"], -item[1]["strength"], current_order.get(item[1]["factorId"], 999), item[1]["factorId"], item[1]["polarity"], item[0]))
    return [item for _index, item in indexed]


def _boolean_preference(record: Mapping[str, Any]) -> bool:
    return bool(record.get("value")) if record.get("polarity") == "like" else not bool(record.get("value"))


def _key_record(value: str) -> Mapping[str, Any] | None:
    try:
        record = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return record if isinstance(record, Mapping) else None


def _same_boolean_preference(exclusion: str, pattern: Mapping[str, Any]) -> bool:
    record = _key_record(exclusion)
    return bool(
        record
        and record.get("factorId") == pattern.get("factorId")
        and isinstance(record.get("value"), bool)
        and record.get("polarity") in {"like", "dislike"}
        and _boolean_preference(record) == bool(pattern.get("value"))
    )


def _is_excluded(exclusions: Iterable[str], pattern: Mapping[str, Any], factor_types: Mapping[str, str]) -> bool:
    values = set(exclusions)
    if pattern["key"] in values:
        return True
    return factor_types.get(str(pattern["factorId"])) == "boolean" and any(_same_boolean_preference(item, pattern) for item in values)


def _selectable_profiles(seed: Mapping[str, Any], verdicts: Mapping[str, str]) -> list[dict[str, Any]]:
    """Compute the canonical ordered profile set the browser may select."""

    candidates = _profile_candidates(seed, verdicts)
    factors = {
        str(factor["id"]): str(factor.get("valueType"))
        for round_value in [*seed.get("history", []), seed.get("round", {})]
        if isinstance(round_value, Mapping)
        for factor in round_value.get("factors", [])
        if isinstance(factor, Mapping) and "id" in factor
    }
    exclusions = list(seed.get("profileExclusions", []))
    candidate_by_key = {item["key"]: item for item in candidates}
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    reserved_exclusive: set[str] = set()

    def add(pattern: Mapping[str, Any]) -> None:
        factor_id = str(pattern["factorId"])
        exclusive = factors.get(factor_id) in {"number", "boolean"}
        if exclusive and factor_id in reserved_exclusive:
            return
        if exclusive:
            reserved_exclusive.add(factor_id)
        if pattern["key"] not in seen and not _is_excluded(exclusions, pattern, factors) and len(selected) < 6:
            seen.add(str(pattern["key"]))
            selected.append(_clone(pattern))

    # The runtime keeps compatible persisted records ahead of newly inferred
    # candidates.  A boolean persisted record survives only with the same
    # semantic preferred value, matching runtime-core's normalized rule.
    for record in seed.get("profilePatterns", []):
        if not isinstance(record, Mapping):
            continue
        factor_type = factors.get(str(record.get("factorId")))
        chosen: Mapping[str, Any] | None = candidate_by_key.get(str(record.get("key")))
        if factor_type == "boolean":
            chosen = next(
                (item for item in candidates if item["factorId"] == record.get("factorId") and bool(item["value"]) == _boolean_preference(record)),
                None,
            )
            if chosen is None:
                continue
        if chosen is None:
            chosen = record
        add(chosen)
    for candidate in candidates:
        add(candidate)
    return selected


def reconstruct_continuation(seed: Mapping[str, Any], *, seed_hash: str, site_url: str, request: BrowserNextRoundRequest, core: Any) -> dict[str, Any]:
    """Build the only continuation accepted from a browser bounded request."""

    expected_ids = [str(option["id"]) for option in seed["round"]["options"]]
    actual_ids = [item.option_id for item in request.verdicts]
    if actual_ids != expected_ids:
        raise ContractError("browser request.verdicts: must cover current option IDs once in canonical order")
    verdict_map = {item.option_id: item.decision for item in request.verdicts}
    selectable = _selectable_profiles(seed, verdict_map)
    selectable_keys = [item["key"] for item in selectable]
    selected = list(request.selected_profile_keys)
    if selected != [key for key in selectable_keys if key in set(selected)]:
        raise ContractError("browser request.selectedProfileKeys: must be a canonical-order subset of the server-derived set")
    selected_set = set(selected)
    selected_patterns = [_clone(item) for item in selectable if item["key"] in selected_set]
    # Exclusions are inherited only from the canonical parent seed.  The
    # browser sends neither opaque keys to add nor free-form prose to remove;
    # its bounded selection decides only which server-derived active records
    # become next-round guidance.  ``_selectable_profiles`` already applies
    # the runtime's normalized boolean exclusion semantics before a key can be
    # selected, including legacy equivalent boolean spellings.
    exclusions = _clone(seed["profileExclusions"])
    continuation = {
        "protocol": "winnow.continuation",
        "schemaVersion": 4,
        "parent": {"sessionId": seed["session"]["id"], "roundNumber": seed["round"]["number"], "seedHash": seed_hash, "url": site_url},
        "session": _clone(seed["session"]),
        "parentProfilePatterns": _clone(seed["profilePatterns"]),
        "parentProfileExclusions": _clone(seed["profileExclusions"]),
        "profilePatterns": selected_patterns,
        "profileExclusions": exclusions,
        "completedRounds": [*_clone(seed["history"]), {**_clone(seed["round"]), "verdicts": [item.as_dict() for item in request.verdicts]}],
        "nextRoundNumber": seed["round"]["number"] + 1,
    }
    try:
        core.validate_continuation(continuation)
    except Exception as exc:
        raise ContractError("browser request cannot produce a valid continuation") from exc
    return continuation


@dataclass(frozen=True)
class CoordinatorConfig:
    max_wait_seconds: int = 300
    renewal_grace_seconds: int = 15
    creation_handoff_seconds: int = 300
    research_deadline_seconds: int = 1_800
    creating_ttl_seconds: int = 900
    daily_quota_limit: int = DEFAULT_DAILY_QUOTA
    allowed_herenow_host_suffixes: tuple[str, ...] = (".here.now",)
    quota_hmac_key: bytes = b"winnow-remote-quota-test-key-must-be-over-32-bytes"

    def __post_init__(self) -> None:
        if self.max_wait_seconds < 1 or self.renewal_grace_seconds < 0 or self.creation_handoff_seconds < 1 or self.research_deadline_seconds < 1 or self.creating_ttl_seconds < 1:
            raise ValueError("coordinator time limits are invalid")
        if self.daily_quota_limit < 1 or len(self.quota_hmac_key) < 32:
            raise ValueError("coordinator quota configuration is invalid")


@dataclass(frozen=True)
class CreationHandle:
    """Internal orchestration handle.  It is never a public tool receipt."""

    session_id: str
    browser_capability: str
    agent_capability: str

    def __repr__(self) -> str:
        return f"CreationHandle(session_id={self.session_id!r}, browser_capability=<redacted>, agent_capability=<redacted>)"


@dataclass(frozen=True)
class PublicationTarget:
    """Server-only update material, deliberately redacted from representations."""

    session_id: str
    browser_capability: str
    slug: str
    site_url: str
    original_expires_at: str
    claim_token: str
    published_revision: int

    def __repr__(self) -> str:
        return (
            "PublicationTarget(session_id=<redacted>, browser_capability=<redacted>, "
            "slug=<redacted>, site_url=<redacted>, original_expires_at=<redacted>, "
            "claim_token=<redacted>, published_revision=<redacted>)"
        )


class Coordinator:
    """Atomic state machine for creation, waits, browser events, and commits."""

    def __init__(
        self,
        repository: TransactionalRepository,
        security: CapabilitySecurity,
        *,
        config: CoordinatorConfig = CoordinatorConfig(),
        now: Callable[[], dt.datetime] = _utc_now,
        core: Any | None = None,
        circuit_mode: str = "normal",
    ) -> None:
        if circuit_mode not in _CIRCUIT_MODES:
            raise ValueError("circuit mode is invalid")
        self._repository = repository
        self._security = security
        self._config = config
        self._now = now
        self._core = core or _load_portable_core()
        self._circuit_mode = circuit_mode

    @property
    def circuit_mode(self) -> str:
        return self._circuit_mode

    def set_circuit_mode(self, mode: str) -> None:
        if mode not in _CIRCUIT_MODES:
            raise ValueError("circuit mode is invalid")
        self._circuit_mode = mode

    def begin_creation(self, seed: Mapping[str, Any], *, network_prefix: str, client_family: str) -> CreationHandle:
        self._require_new_session_circuit()
        self._validate_seed(seed, MAX_REMOTE_CREATE_SEED_BYTES, label="create seed")
        if seed["round"]["number"] != 1 or seed["history"]:
            raise CoordinatorError("rolling creation requires round one")
        now = self._now_timestamp()
        bucket, quota_expiry = self._quota_bucket(network_prefix, client_family)
        if not self._repository.increment_quota(bucket, expires_at=quota_expiry, limit=self._config.daily_quota_limit):
            raise QuotaExceeded("creation quota is exhausted")
        session_id = secrets.token_urlsafe(24)
        # The browser bearer is reproducible only by this service's secret,
        # rather than stored alongside its keyed hash.  A restart can therefore
        # compile a successor page without retaining a raw public bearer.
        browser_capability = self._security.browser_capability_for_session(session_id)
        agent_capability = self._security.new_capability()
        record = ActiveSession(
            record_version=0,
            session_id=session_id,
            browser_capability_hash=self._security.capability_hash(browser_capability),
            agent_capability_hash=self._security.capability_hash(agent_capability),
            seed=_clone(dict(seed)),
            seed_hash=self._core.seed_hash(dict(seed)),
            current_round_number=1,
            published_revision=0,
            phase="creating",
            created_at=now,
            original_expires_at=None,
            expires_at=now + self._config.creating_ttl_seconds,
        )
        self._assert_record_size(record)
        self._repository.create(record)
        return CreationHandle(session_id=session_id, browser_capability=browser_capability, agent_capability=agent_capability)

    def persist_creation_publication(
        self,
        handle: CreationHandle,
        *,
        site_url: str,
        slug: str,
        original_expires_at: str,
        claim_token: str,
        pending_publication: Mapping[str, Any] | None = None,
    ) -> None:
        """Persist the encrypted provider claim before a final upload/finalize."""

        origin_url, origin = normalize_public_origin(site_url, allowed_host_suffixes=self._config.allowed_herenow_host_suffixes)
        if not isinstance(slug, str) or not slug or len(slug) > 128:
            raise CoordinatorError("HereNow slug is invalid")
        expiry = _parse_iso_timestamp(original_expires_at)
        if expiry <= self._now_timestamp():
            raise CoordinatorError("HereNow expiration is already elapsed")
        encrypted = self._security.encrypt_claim_token(session_id=handle.session_id, claim_token=claim_token)
        pending = self._bounded_internal_mapping(pending_publication, "pending publication") if pending_publication is not None else None

        def mutate(record: ActiveSession) -> tuple[None, ActiveSession]:
            if record.phase != "creating":
                raise StateConflict("creation is no longer pending")
            record.site_url = origin_url
            record.slug = slug
            record.allowed_origin = origin
            record.original_expires_at = original_expires_at
            record.expires_at = expiry
            record.encrypted_claim_token = encrypted.as_dict()
            record.pending_publication = pending
            return None, record

        self._mutate_by_session(handle.session_id, mutate)

    def activate_creation(self, handle: CreationHandle, *, published_revision: int = 1) -> dict[str, Any]:
        if published_revision != 1:
            raise CoordinatorError("initial published revision must be one")

        def mutate(record: ActiveSession) -> tuple[dict[str, Any], ActiveSession]:
            if record.phase != "creating" or not record.site_url or not record.allowed_origin or not record.encrypted_claim_token or not record.original_expires_at:
                raise StateConflict("creation has not persisted all publication state")
            record.phase = "awaiting_agent"
            record.published_revision = 1
            record.creation_handoff_deadline = self._now_timestamp() + self._config.creation_handoff_seconds
            record.pending_publication = None
            return self._create_receipt(record, handle.agent_capability), record

        return self._mutate_by_session(handle.session_id, mutate)

    def wait_for_continue(self, session_handle: str, request: WaitForContinueRequest) -> dict[str, Any]:
        capability_hash = self._security.capability_hash(session_handle)

        def mutate(record: ActiveSession) -> tuple[dict[str, Any], ActiveSession | TerminalTombstone | None]:
            current = self._terminal_if_due(record)
            if isinstance(current, TerminalTombstone):
                return self._terminal_wait(current), current
            record = current
            self._require_expected(record, request.expected_round_number, request.expected_seed_hash)
            if self._circuit_mode in {"read_only_existing", "status_only"}:
                tombstone = self._tombstone(record, "circuit_open", "circuit_transition")
                return self._terminal_wait(tombstone), tombstone
            if self._remaining_capacity(record.seed) < 4:
                tombstone = self._tombstone(record, "complete", "option_capacity")
                return self._terminal_wait(tombstone), tombstone
            now = self._now_timestamp()
            if record.phase == "research_requested" and record.accepted_event:
                record.agent_state = "event_delivered"
                record.accepted_event["deliveryAttempts"] = int(record.accepted_event.get("deliveryAttempts", 0)) + 1
                return self._event_wait(record), record
            if record.phase == "publishing":
                return {"status": "publishing", "roundNumber": record.current_round_number, "seedHash": record.seed_hash}, None
            if record.phase != "awaiting_agent" and record.phase != "accepting_request":
                raise StateConflict("session cannot register a wait in its current phase")
            if record.phase == "accepting_request" and record.agent_state != "waiting":
                raise StateConflict("session wait state is invalid")
            if record.phase == "awaiting_agent":
                record.phase = "accepting_request"
                record.wait_epoch += 1
            record.agent_state = "waiting"
            record.wait_deadline = now + request.max_wait_seconds
            record.wait_grace_ends_at = record.wait_deadline + self._config.renewal_grace_seconds
            record.creation_handoff_deadline = None
            return {
                "status": "still_waiting",
                "waitEpoch": record.wait_epoch,
                "roundNumber": record.current_round_number,
                "seedHash": record.seed_hash,
                "expiresAt": record.original_expires_at,
            }, record

        return self._mutate_by_capability("agent", capability_hash, mutate)

    def poll_wait_for_continue(self, session_handle: str, request: WaitForContinueRequest) -> dict[str, Any] | None:
        """Read a registered wait without extending its bounded deadline.

        The transport calls this after notification wakeups and bounded polling.
        It never consumes an accepted event: a network cancellation before a
        response is delivered leaves the same event/fence available to retry.
        """

        capability_hash = self._security.capability_hash(session_handle)

        def mutate(record: ActiveSession) -> tuple[dict[str, Any] | None, ActiveSession | TerminalTombstone | None]:
            current = self._terminal_if_due(record)
            if isinstance(current, TerminalTombstone):
                return self._terminal_wait(current), current
            record = current
            self._require_expected(record, request.expected_round_number, request.expected_seed_hash)
            if record.phase == "research_requested" and record.accepted_event:
                record.agent_state = "event_delivered"
                record.accepted_event["deliveryAttempts"] = int(record.accepted_event.get("deliveryAttempts", 0)) + 1
                return self._event_wait(record), record
            if record.phase == "publishing":
                return {"status": "publishing", "roundNumber": record.current_round_number, "seedHash": record.seed_hash}, None
            if record.phase == "accepting_request" and record.agent_state == "waiting":
                return None, record if current is not record else None
            if record.phase == "awaiting_agent":
                return None, None
            raise StateConflict("session wait state is invalid")

        return self._mutate_by_capability("agent", capability_hash, mutate)

    def browser_cors_origin(self, browser_capability: str, *, origin: str) -> str:
        """Authorize only the exact CORS origin without exposing state."""

        capability_hash = self._security.capability_hash(browser_capability)
        stored = self._repository.lookup_browser(capability_hash)
        if stored is None:
            raise AuthenticationError("capability is invalid")
        self._require_origin(stored, origin)
        return origin

    def browser_wait_notification_key(self, browser_capability: str, *, origin: str) -> str | None:
        """Resolve the private waiter channel only after browser authorization."""

        capability_hash = self._security.capability_hash(browser_capability)
        stored = self._repository.lookup_browser(capability_hash)
        if not isinstance(stored, ActiveSession):
            return None
        self._require_origin(stored, origin)
        return stored.session_id

    def agent_wait_notification_key(self, session_handle: str) -> str | None:
        """Resolve the private waiter channel for an authenticated agent only."""

        capability_hash = self._security.capability_hash(session_handle)
        stored = self._repository.lookup_agent(capability_hash)
        if stored is None:
            raise AuthenticationError("capability is invalid")
        return stored.session_id if isinstance(stored, ActiveSession) else None

    def browser_status(self, browser_capability: str, *, origin: str, embedded_revision: int) -> dict[str, Any]:
        capability_hash = self._security.capability_hash(browser_capability)
        terminal = self._browser_tombstone(capability_hash, origin)
        if terminal is not None:
            return self._terminal_browser(terminal)

        def mutate(record: ActiveSession) -> tuple[dict[str, Any], ActiveSession | TerminalTombstone | None]:
            self._require_origin(record, origin)
            current = self._terminal_if_due(record)
            if isinstance(current, TerminalTombstone):
                return self._terminal_browser(current), current
            record = current
            status = self._browser_phase_status(record, embedded_revision)
            return {
                "status": status,
                "roundNumber": record.current_round_number,
                "seedHash": record.seed_hash,
                "publishedRevision": record.published_revision,
                "expiresAt": record.original_expires_at,
                "agentLeaseExpiresAt": _to_iso(record.wait_deadline),
                "remainingOptionCapacity": self._remaining_capacity(record.seed),
                "corsOrigin": record.allowed_origin,
            }, record if current is not record else None

        return self._mutate_by_capability("browser", capability_hash, mutate)

    def accept_browser_next_round(self, browser_capability: str, *, origin: str, request: BrowserNextRoundRequest) -> dict[str, Any]:
        capability_hash = self._security.capability_hash(browser_capability)
        terminal = self._browser_tombstone(capability_hash, origin)
        if terminal is not None:
            return {"status": terminal.terminal_status, "corsOrigin": terminal.allowed_origin}

        def mutate(record: ActiveSession) -> tuple[dict[str, Any], ActiveSession | TerminalTombstone | None]:
            self._require_origin(record, origin)
            current = self._terminal_if_due(record)
            if isinstance(current, TerminalTombstone):
                return {"status": current.terminal_status, "corsOrigin": current.allowed_origin}, current
            record = current
            existing = record.idempotency.get(request.idempotency_key)
            if existing is not None:
                if not hmac.compare_digest(str(existing["digest"]), request.digest):
                    raise StateConflict("browser idempotency key was reused with a different request")
                return _clone(existing["result"]), record if current is not record else None
            if self._circuit_mode in {"read_only_existing", "status_only"}:
                tombstone = self._tombstone(record, "circuit_open", "circuit_transition")
                return {"status": "circuit_open", "corsOrigin": tombstone.allowed_origin}, tombstone
            if self._circuit_mode == "no_new_sessions":
                # Existing sessions may finish normally in this mode.
                pass
            self._require_expected(record, request.round_number, request.seed_hash, request.published_revision)
            if record.phase != "accepting_request" or record.agent_state != "waiting" or record.wait_deadline is None or self._now_timestamp() > record.wait_deadline:
                raise StateConflict("agent is not actively waiting for this revision")
            if self._remaining_capacity(record.seed) < 4:
                tombstone = self._tombstone(record, "complete", "option_capacity")
                return {"status": "complete", "corsOrigin": tombstone.allowed_origin}, tombstone
            if not record.site_url:
                raise StateConflict("active publication URL is unavailable")
            continuation = reconstruct_continuation(record.seed, seed_hash=record.seed_hash, site_url=record.site_url, request=request, core=self._core)
            self._assert_mcp_result_size(
                continuation,
                limit=MAX_REMOTE_CONTINUATION_HANDOFF_BYTES,
            )
            event_id = secrets.token_urlsafe(24)
            publish_fence = secrets.token_urlsafe(32)
            result = {"status": "accepted", "roundNumber": record.current_round_number, "publishedRevision": record.published_revision, "corsOrigin": record.allowed_origin}
            record.phase = "research_requested"
            record.agent_state = "event_delivered"
            record.accepted_event = {
                "eventId": event_id,
                "publishFence": publish_fence,
                "continuation": continuation,
                "deliveryAttempts": 0,
                "requestDigest": request.digest,
            }
            record.idempotency = {request.idempotency_key: {"digest": request.digest, "result": _clone(result)}}
            record.publish_fence = publish_fence
            record.research_deadline = self._now_timestamp() + self._config.research_deadline_seconds
            record.wait_deadline = None
            record.wait_grace_ends_at = None
            return result, record

        return self._mutate_by_capability("browser", capability_hash, mutate)

    def begin_publish(self, session_handle: str, request: PublishNextRoundRequest) -> dict[str, Any]:
        """Acquire the one fenced publication lease before provider I/O."""

        capability_hash = self._security.capability_hash(session_handle)

        def mutate(record: ActiveSession) -> tuple[dict[str, Any], ActiveSession | TerminalTombstone | None]:
            current = self._terminal_if_due(record)
            if isinstance(current, TerminalTombstone):
                return self._terminal_wait(current), current
            record = current
            if record.phase != "research_requested":
                raise StateConflict("publish event is already owned or unavailable")
            self._require_publish_event(record, request)
            if record.research_deadline is not None and self._now_timestamp() > record.research_deadline:
                tombstone = self._tombstone(record, "disconnected", "research_deadline")
                return self._terminal_wait(tombstone), tombstone
            self._validate_successor(record, request)
            record.phase = "publishing"
            record.pending_seed = _clone(dict(request.next_seed))
            record.pending_seed_hash = request.next_seed_hash
            return {"status": "publishing", "publishedRevision": record.published_revision + 1}, record

        return self._mutate_by_capability("agent", capability_hash, mutate)

    def publication_target(self, session_handle: str, request: PublishNextRoundRequest) -> PublicationTarget:
        """Return only the server-internal state needed to update HereNow."""

        capability_hash = self._security.capability_hash(session_handle)
        stored = self._repository.lookup_agent(capability_hash)
        if not isinstance(stored, ActiveSession):
            if isinstance(stored, TerminalTombstone):
                raise StateConflict("publication is terminal")
            raise AuthenticationError("capability is invalid")
        if stored.phase != "publishing":
            raise StateConflict("publication is not active")
        self._require_publish_event(stored, request)
        if not all((stored.slug, stored.site_url, stored.original_expires_at, stored.encrypted_claim_token)):
            raise StateConflict("publication target is incomplete")
        try:
            encrypted = EncryptedSecret.from_dict(stored.encrypted_claim_token)
            claim_token = self._security.decrypt_claim_token(session_id=stored.session_id, encrypted=encrypted)
            browser_capability = self._security.browser_capability_for_session(stored.session_id)
        except SecretError:
            raise CoordinatorError("publication secret is unavailable") from None
        return PublicationTarget(
            session_id=stored.session_id,
            browser_capability=browser_capability,
            slug=stored.slug,
            site_url=stored.site_url,
            original_expires_at=stored.original_expires_at,
            claim_token=claim_token,
            published_revision=stored.published_revision + 1,
        )

    def persist_pending_publication(self, session_handle: str, *, publish_fence: str, pending_publication: Mapping[str, Any]) -> None:
        """Store provider version metadata before upload/finalize for restart recovery."""

        capability_hash = self._security.capability_hash(session_handle)
        pending = self._bounded_internal_mapping(pending_publication, "pending publication")

        def mutate(record: ActiveSession) -> tuple[None, ActiveSession]:
            if record.phase != "publishing" or not record.publish_fence or not hmac.compare_digest(record.publish_fence, publish_fence):
                raise StateConflict("publish fence is stale")
            record.pending_publication = pending
            return None, record

        self._mutate_by_capability("agent", capability_hash, mutate)

    def pending_publication_metadata(self, session_handle: str, request: PublishNextRoundRequest) -> dict[str, Any] | None:
        """Return persisted, non-secret update metadata for a fenced recovery.

        ``publishing`` is durable specifically so a replacement worker can
        reconcile an upload/finalize result before it considers another update.
        The returned mapping contains provider version identity and expected
        public markers only; claim material remains available solely through
        :meth:`publication_target`.
        """

        capability_hash = self._security.capability_hash(session_handle)
        stored = self._repository.lookup_agent(capability_hash)
        if not isinstance(stored, ActiveSession):
            if isinstance(stored, TerminalTombstone):
                return None
            raise AuthenticationError("capability is invalid")
        if stored.phase != "publishing":
            return None
        self._require_publish_event(stored, request)
        return _clone(stored.pending_publication) if stored.pending_publication is not None else {}

    def retry_pending_publication(self, session_handle: str, request: PublishNextRoundRequest) -> None:
        """Release one unreconciled fenced publish for a same-event retry.

        Callers must first fetch the persisted live markers.  A false marker
        result means the old provider version cannot be treated as committed;
        clearing only pending provider metadata lets the *same* event/fence
        create a fresh update.  It never creates a second accepted browser
        event or allows a different successor to take ownership.
        """

        capability_hash = self._security.capability_hash(session_handle)

        def mutate(record: ActiveSession) -> tuple[None, ActiveSession | TerminalTombstone | None]:
            current = self._terminal_if_due(record)
            if isinstance(current, TerminalTombstone):
                return None, current
            record = current
            if record.phase != "publishing":
                raise StateConflict("publication is not pending recovery")
            self._require_publish_event(record, request)
            record.phase = "research_requested"
            record.pending_publication = None
            record.pending_seed = None
            record.pending_seed_hash = None
            return None, record

        self._mutate_by_capability("agent", capability_hash, mutate)

    def commit_publish(self, session_handle: str, request: PublishNextRoundRequest) -> dict[str, Any]:
        """Commit one verified provider revision after its external update succeeds."""

        capability_hash = self._security.capability_hash(session_handle)

        def mutate(record: ActiveSession) -> tuple[dict[str, Any], ActiveSession | TerminalTombstone | None]:
            current = self._terminal_if_due(record)
            if isinstance(current, TerminalTombstone):
                return self._terminal_wait(current), current
            record = current
            if record.phase != "publishing" or record.pending_seed_hash is None or not hmac.compare_digest(record.pending_seed_hash, request.next_seed_hash):
                raise StateConflict("publication is not owned by this successor")
            self._require_publish_event(record, request)
            self._validate_successor(record, request)
            record.seed = _clone(dict(request.next_seed))
            record.seed_hash = self._core.seed_hash(record.seed)
            record.current_round_number = int(record.seed["round"]["number"])
            record.published_revision += 1
            record.phase = "awaiting_agent"
            record.agent_state = "disconnected"
            record.wait_deadline = None
            record.wait_grace_ends_at = None
            record.accepted_event = None
            record.publish_fence = None
            record.pending_publication = None
            record.pending_seed = None
            record.pending_seed_hash = None
            record.research_deadline = None
            record.idempotency = {}
            return {
                "status": "awaiting_agent_wait",
                "roundNumber": record.current_round_number,
                "seedHash": record.seed_hash,
                "publishedRevision": record.published_revision,
                "expiresAt": record.original_expires_at,
            }, record

        return self._mutate_by_capability("agent", capability_hash, mutate)

    def fail_session(self, session_handle: str, *, category: str = "publication_failure") -> dict[str, Any]:
        """Irreversibly sanitize an active session after a nonrecoverable failure."""

        capability_hash = self._security.capability_hash(session_handle)
        if category not in {"publication_failure", "validation_failure", "operator_failure", "research_deadline"}:
            raise CoordinatorError("terminal category is invalid")

        def mutate(record: ActiveSession) -> tuple[dict[str, Any], TerminalTombstone]:
            tombstone = self._tombstone(record, "failed", category)
            return self._terminal_wait(tombstone), tombstone

        return self._mutate_by_capability("agent", capability_hash, mutate)

    def expire_due(self, session_id: str) -> dict[str, Any] | None:
        """Run deterministic expiry/grace cleanup; safe for a periodic worker."""

        record = self._repository.get(session_id)
        if not isinstance(record, ActiveSession):
            return None

        def mutate(current: ActiveSession) -> tuple[dict[str, Any] | None, ActiveSession | TerminalTombstone | None]:
            terminal = self._terminal_if_due(current)
            if isinstance(terminal, TerminalTombstone):
                return {"status": terminal.terminal_status}, terminal
            return None, None

        return self._mutate_by_session(session_id, mutate)

    def _browser_tombstone(self, capability_hash: str, origin: str) -> TerminalTombstone | None:
        """Authorize terminal browser reads before exposing their safe status."""

        stored = self._repository.lookup_browser(capability_hash)
        if stored is None:
            raise AuthenticationError("capability is invalid")
        if isinstance(stored, TerminalTombstone):
            self._require_origin(stored, origin)
            return stored
        return None

    def _mutate_by_capability(self, kind: str, capability_hash: str, mutation: Callable[[ActiveSession], tuple[Any, ActiveSession | TerminalTombstone | None]]) -> Any:
        lookup = self._repository.lookup_browser if kind == "browser" else self._repository.lookup_agent
        for _attempt in range(12):
            stored = lookup(capability_hash)
            if stored is None:
                raise AuthenticationError("capability is invalid")
            if isinstance(stored, TerminalTombstone):
                if kind == "browser":
                    # Browser methods verify Origin before they expose even a
                    # terminal status.  Waits have no Origin input.
                    raise StateConflict("terminal browser state requires an origin-aware operation")
                return self._terminal_wait(stored)
            record = _clone(stored)
            result, replacement = mutation(record)
            if replacement is None:
                return result
            self._assert_record_size(replacement)
            replacement.record_version = stored.record_version + 1
            if self._repository.compare_and_swap(stored.session_id, stored.record_version, replacement):
                return result
        raise StateConflict("concurrent state transition did not settle")

    def _mutate_by_session(self, session_id: str, mutation: Callable[[ActiveSession], tuple[Any, ActiveSession | TerminalTombstone | None]]) -> Any:
        for _attempt in range(12):
            stored = self._repository.get(session_id)
            if not isinstance(stored, ActiveSession):
                raise StateConflict("active session is unavailable")
            record = _clone(stored)
            result, replacement = mutation(record)
            if replacement is None:
                return result
            self._assert_record_size(replacement)
            replacement.record_version = stored.record_version + 1
            if self._repository.compare_and_swap(session_id, stored.record_version, replacement):
                return result
        raise StateConflict("concurrent state transition did not settle")

    def _terminal_if_due(self, record: ActiveSession) -> ActiveSession | TerminalTombstone:
        now = self._now_timestamp()
        if now >= record.expires_at:
            return self._tombstone(record, "expired", "public_expiry")
        if record.phase == "creating":
            return record
        if record.phase == "awaiting_agent" and record.creation_handoff_deadline is not None and now > record.creation_handoff_deadline:
            return self._tombstone(record, "disconnected", "creation_handoff_expired")
        if record.phase == "accepting_request" and record.wait_grace_ends_at is not None and now > record.wait_grace_ends_at:
            return self._tombstone(record, "disconnected", "wait_renewal_expired")
        if record.phase in {"research_requested", "publishing"} and record.research_deadline is not None and now > record.research_deadline:
            return self._tombstone(record, "disconnected", "research_deadline")
        return record

    @staticmethod
    def _tombstone(record: ActiveSession, status: str, category: str) -> TerminalTombstone:
        if status not in _TERMINAL_STATUSES:
            raise CoordinatorError("terminal status is invalid")
        return TerminalTombstone(
            record_version=record.record_version,
            session_id=record.session_id,
            browser_capability_hash=record.browser_capability_hash,
            agent_capability_hash=record.agent_capability_hash,
            allowed_origin=record.allowed_origin,
            original_expires_at=record.original_expires_at,
            expires_at=record.expires_at,
            terminal_status=status,
            terminal_category=category,
        )

    def _require_expected(self, record: ActiveSession, round_number: int, seed_hash: str, revision: int | None = None) -> None:
        if round_number != record.current_round_number or not hmac.compare_digest(seed_hash, record.seed_hash):
            raise StateConflict("expected round or seed hash is stale")
        if revision is not None and revision != record.published_revision:
            raise StateConflict("expected published revision is stale")

    @staticmethod
    def _require_origin(record: ActiveSession | TerminalTombstone, origin: str) -> None:
        if not record.allowed_origin or not isinstance(origin, str) or not hmac.compare_digest(origin, record.allowed_origin):
            raise AuthenticationError("browser origin is not authorized for this session")

    def _require_publish_event(self, record: ActiveSession, request: PublishNextRoundRequest) -> None:
        if record.phase not in {"research_requested", "publishing"} or not record.accepted_event or not record.publish_fence:
            raise StateConflict("no publishable event is active")
        event = record.accepted_event
        if not hmac.compare_digest(str(event.get("eventId", "")), request.event_id) or not hmac.compare_digest(record.publish_fence, request.publish_fence):
            raise StateConflict("event or publish fence is stale")
        if not hmac.compare_digest(record.seed_hash, request.parent_seed_hash):
            raise StateConflict("publish parent seed hash is stale")

    def _validate_successor(self, record: ActiveSession, request: PublishNextRoundRequest) -> None:
        if not record.accepted_event:
            raise StateConflict("accepted continuation is unavailable")
        self._validate_seed(request.next_seed, MAX_REMOTE_SUCCESSOR_SEED_BYTES, label="successor seed")
        if self._total_options(request.next_seed) > MAX_SESSION_OPTIONS:
            raise CoordinatorError("successor exceeds the cumulative option capacity")
        try:
            self._core.validate_successor(record.accepted_event["continuation"], dict(request.next_seed))
        except Exception as exc:
            raise CoordinatorError("successor does not match the accepted continuation") from exc

    def _validate_seed(self, seed: Mapping[str, Any], limit: int, *, label: str) -> None:
        try:
            encoded = canonical_json(dict(seed))
        except ContractError as exc:
            raise CoordinatorError(f"{label} cannot be serialized") from exc
        if len(encoded) > limit:
            raise CoordinatorError(f"{label} exceeds the byte limit")
        try:
            self._core.validate_seed(dict(seed))
        except Exception as exc:
            raise CoordinatorError(f"{label} is invalid") from exc

    @staticmethod
    def _total_options(seed: Mapping[str, Any]) -> int:
        return sum(len(round_value.get("options", [])) for round_value in [*seed.get("history", []), seed.get("round", {})] if isinstance(round_value, Mapping))

    def _remaining_capacity(self, seed: Mapping[str, Any]) -> int:
        return max(0, MAX_SESSION_OPTIONS - self._total_options(seed))

    def _browser_phase_status(self, record: ActiveSession, embedded_revision: int) -> str:
        if self._circuit_mode == "status_only":
            return "circuit_open"
        if embedded_revision < record.published_revision:
            return "ready_to_reveal"
        if record.phase == "accepting_request":
            if record.wait_deadline is not None and self._now_timestamp() <= record.wait_deadline:
                return "connected"
            return "connecting"
        if record.phase in {"research_requested", "publishing"}:
            return "researching"
        return "connecting"

    @staticmethod
    def _terminal_wait(tombstone: TerminalTombstone) -> dict[str, Any]:
        return {"status": tombstone.terminal_status, "expiresAt": tombstone.original_expires_at}

    @staticmethod
    def _terminal_browser(tombstone: TerminalTombstone) -> dict[str, Any]:
        return {"status": tombstone.terminal_status, "expiresAt": tombstone.original_expires_at, "corsOrigin": tombstone.allowed_origin}

    def _event_wait(self, record: ActiveSession) -> dict[str, Any]:
        event = record.accepted_event
        assert event is not None
        return {
            "status": "continue_requested",
            "eventId": event["eventId"],
            "publishFence": event["publishFence"],
            "continuation": _clone(event["continuation"]),
            "remainingOptionCapacity": self._remaining_capacity(record.seed),
            "researchDeadline": _to_iso(record.research_deadline),
            "expiresAt": record.original_expires_at,
        }

    @staticmethod
    def _bounded_internal_mapping(value: Mapping[str, Any], label: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise CoordinatorError(f"{label} is invalid")
        try:
            encoded = canonical_json(dict(value))
        except ContractError as exc:
            raise CoordinatorError(f"{label} is invalid") from exc
        if len(encoded) > MAX_REMOTE_STORED_RECORD_BYTES // 2:
            raise CoordinatorError(f"{label} exceeds the byte limit")
        if Coordinator._contains_secret_field(value):
            raise CoordinatorError(f"{label} must not contain capability or claim material")
        return _clone(dict(value))

    @staticmethod
    def _contains_secret_field(value: Any) -> bool:
        if isinstance(value, Mapping):
            for key, child in value.items():
                normalized = str(key).replace("_", "").lower()
                if normalized in {"claimtoken", "browsercapability", "agentcapability", "sessionhandle"}:
                    return True
                if Coordinator._contains_secret_field(child):
                    return True
        elif isinstance(value, list):
            return any(Coordinator._contains_secret_field(item) for item in value)
        return False

    def _assert_record_size(self, record: StoredSession) -> None:
        try:
            encoded = canonical_json(record.as_dict())
        except ContractError as exc:
            raise CoordinatorError("stored record cannot be serialized") from exc
        if len(encoded) > MAX_REMOTE_STORED_RECORD_BYTES:
            raise CoordinatorError("stored record exceeds the byte limit")

    @staticmethod
    def _assert_mcp_result_size(value: Mapping[str, Any], *, limit: int = MAX_REMOTE_MCP_RESULT_BYTES) -> None:
        try:
            encoded = canonical_json(dict(value))
        except ContractError as exc:
            raise CoordinatorError("MCP result cannot be serialized") from exc
        if len(encoded) > limit:
            raise CoordinatorError("MCP result exceeds the byte limit")

    def _create_receipt(self, record: ActiveSession, agent_capability: str) -> dict[str, Any]:
        return {
            "sessionHandle": agent_capability,
            "siteUrl": record.site_url,
            "expiresAt": record.original_expires_at,
            "roundNumber": record.current_round_number,
            "seedHash": record.seed_hash,
            "publishedRevision": record.published_revision,
            "agentHandoffExpiresAt": _to_iso(record.creation_handoff_deadline),
            "status": "awaiting_agent_wait",
        }

    def _require_new_session_circuit(self) -> None:
        if self._circuit_mode != "normal":
            raise CircuitOpen("new sessions are unavailable while the circuit is open")

    def _quota_bucket(self, network_prefix: str, client_family: str) -> tuple[str, float]:
        if not isinstance(network_prefix, str) or not network_prefix or len(network_prefix) > 128 or any(character.isspace() for character in network_prefix):
            raise CoordinatorError("trusted normalized network prefix is required")
        try:
            parsed = ipaddress.ip_network(network_prefix, strict=True)
        except ValueError as exc:
            raise CoordinatorError("trusted normalized network prefix is required") from exc
        expected_prefix = 24 if isinstance(parsed, ipaddress.IPv4Network) else 64
        if parsed.prefixlen != expected_prefix or str(parsed) != network_prefix:
            raise CoordinatorError("trusted normalized network prefix is required")
        family = client_family if client_family in {"anthropic", "openai", "other"} else "other"
        now = self._now().astimezone(dt.timezone.utc)
        date = now.date()
        next_boundary = dt.datetime.combine(date + dt.timedelta(days=1), dt.time.min, tzinfo=dt.timezone.utc).timestamp() + 3600
        digest = hmac.new(self._config.quota_hmac_key, f"{date.isoformat()}\0{network_prefix}\0{family}".encode("utf-8"), hashlib.sha256).hexdigest()
        return digest, next_boundary

    def _now_timestamp(self) -> float:
        return _to_timestamp(self._now())
