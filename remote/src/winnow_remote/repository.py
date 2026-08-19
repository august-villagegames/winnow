"""Redis-compatible durable state records and a deterministic transactional fake.

The coordinator performs compare-and-set mutations against this small
repository interface.  It deliberately does not grow a storage plug-in layer:
``RedisRepository`` is the production shape and ``FakeRepository`` exists only
to make transition tests deterministic without a Redis server.
"""

from __future__ import annotations

import copy
import json
import threading
from dataclasses import asdict, dataclass, field
from typing import Any, Mapping, Protocol


class RepositoryError(RuntimeError):
    """A bounded repository-level state failure."""


class RecordConflict(RepositoryError):
    """A competing transition changed a record before it could be committed."""


@dataclass
class ActiveSession:
    """The complete active state.  It is serialized only inside the repository."""

    record_version: int
    session_id: str
    browser_capability_hash: str
    agent_capability_hash: str
    seed: dict[str, Any]
    seed_hash: str
    current_round_number: int
    published_revision: int
    phase: str
    created_at: float
    original_expires_at: str | None
    expires_at: float
    site_url: str | None = None
    slug: str | None = None
    allowed_origin: str | None = None
    encrypted_claim_token: dict[str, str] | None = None
    creation_handoff_deadline: float | None = None
    agent_state: str = "disconnected"
    wait_epoch: int = 0
    wait_deadline: float | None = None
    wait_grace_ends_at: float | None = None
    accepted_event: dict[str, Any] | None = None
    idempotency: dict[str, dict[str, Any]] = field(default_factory=dict)
    publish_fence: str | None = None
    pending_publication: dict[str, Any] | None = None
    pending_seed: dict[str, Any] | None = None
    pending_seed_hash: str | None = None
    research_deadline: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return {"kind": "active", **asdict(self)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "ActiveSession":
        if value.get("kind") != "active":
            raise RepositoryError("stored record has an unexpected kind")
        fields = {key: copy.deepcopy(item) for key, item in value.items() if key != "kind"}
        return cls(**fields)


@dataclass
class TerminalTombstone:
    """Terminal-only state, intentionally without content or mutation secrets."""

    record_version: int
    session_id: str
    browser_capability_hash: str
    agent_capability_hash: str
    allowed_origin: str | None
    original_expires_at: str | None
    expires_at: float
    terminal_status: str
    terminal_category: str

    def as_dict(self) -> dict[str, Any]:
        return {"kind": "tombstone", **asdict(self)}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "TerminalTombstone":
        if value.get("kind") != "tombstone":
            raise RepositoryError("stored record has an unexpected kind")
        fields = {key: copy.deepcopy(item) for key, item in value.items() if key != "kind"}
        return cls(**fields)


StoredSession = ActiveSession | TerminalTombstone


def record_from_dict(value: Mapping[str, Any]) -> StoredSession:
    kind = value.get("kind")
    if kind == "active":
        return ActiveSession.from_dict(value)
    if kind == "tombstone":
        return TerminalTombstone.from_dict(value)
    raise RepositoryError("stored record has an unexpected kind")


class TransactionalRepository(Protocol):
    """Minimal atomic operations needed by the coordinator.

    ``compare_and_swap`` must update the record and both capability indexes in
    one transaction.  Callers retry on a false return; they never perform a
    read-modify-write against an unfenced record.
    """

    def create(self, record: ActiveSession) -> None: ...

    def lookup_browser(self, capability_hash: str) -> StoredSession | None: ...

    def lookup_agent(self, capability_hash: str) -> StoredSession | None: ...

    def get(self, session_id: str) -> StoredSession | None: ...

    def compare_and_swap(self, session_id: str, expected_record_version: int, replacement: StoredSession) -> bool: ...

    def increment_quota(self, bucket: str, *, expires_at: float, limit: int) -> bool: ...

    def increment_rate_limit(self, bucket: str, *, expires_at: float, limit: int) -> bool: ...


class FakeRepository:
    """Deterministic fake with the same CAS and TTL semantics as Redis.

    It accepts a clock callback so domain tests can cross every expiry/grace
    edge without sleeps.  It is not a second production backend.
    """

    def __init__(self, *, now: callable) -> None:
        self._now = now
        self._lock = threading.RLock()
        self._records: dict[str, dict[str, Any]] = {}
        self._browser_index: dict[str, str] = {}
        self._agent_index: dict[str, str] = {}
        self._quotas: dict[str, tuple[int, float]] = {}
        self._rate_limits: dict[str, tuple[int, float]] = {}

    def create(self, record: ActiveSession) -> None:
        with self._lock:
            self._purge_expired()
            if record.session_id in self._records:
                raise RecordConflict("session already exists")
            if record.browser_capability_hash in self._browser_index or record.agent_capability_hash in self._agent_index:
                raise RecordConflict("capability index already exists")
            self._records[record.session_id] = copy.deepcopy(record.as_dict())
            self._browser_index[record.browser_capability_hash] = record.session_id
            self._agent_index[record.agent_capability_hash] = record.session_id

    def lookup_browser(self, capability_hash: str) -> StoredSession | None:
        with self._lock:
            self._purge_expired()
            return self._lookup(self._browser_index.get(capability_hash))

    def lookup_agent(self, capability_hash: str) -> StoredSession | None:
        with self._lock:
            self._purge_expired()
            return self._lookup(self._agent_index.get(capability_hash))

    def get(self, session_id: str) -> StoredSession | None:
        with self._lock:
            self._purge_expired()
            return self._lookup(session_id)

    def compare_and_swap(self, session_id: str, expected_record_version: int, replacement: StoredSession) -> bool:
        with self._lock:
            self._purge_expired()
            current = self._records.get(session_id)
            if current is None or int(current.get("record_version", -1)) != expected_record_version:
                return False
            if replacement.session_id != session_id:
                raise RepositoryError("session ID cannot change")
            if replacement.record_version != expected_record_version + 1:
                raise RepositoryError("replacement record version is invalid")
            self._records[session_id] = copy.deepcopy(replacement.as_dict())
            self._browser_index[replacement.browser_capability_hash] = session_id
            self._agent_index[replacement.agent_capability_hash] = session_id
            return True

    def increment_quota(self, bucket: str, *, expires_at: float, limit: int) -> bool:
        return self._increment_counter(self._quotas, bucket, expires_at=expires_at, limit=limit)

    def increment_rate_limit(self, bucket: str, *, expires_at: float, limit: int) -> bool:
        return self._increment_counter(self._rate_limits, bucket, expires_at=expires_at, limit=limit)

    def _increment_counter(self, counters: dict[str, tuple[int, float]], bucket: str, *, expires_at: float, limit: int) -> bool:
        if not isinstance(bucket, str) or not bucket or limit < 1:
            raise RepositoryError("counter input is invalid")
        with self._lock:
            self._purge_expired()
            count, existing_expiry = counters.get(bucket, (0, expires_at))
            if existing_expiry <= self._now():
                count, existing_expiry = 0, expires_at
            if count >= limit:
                return False
            counters[bucket] = (count + 1, existing_expiry)
            return True

    def _lookup(self, session_id: str | None) -> StoredSession | None:
        if session_id is None:
            return None
        value = self._records.get(session_id)
        return record_from_dict(copy.deepcopy(value)) if value is not None else None

    def _purge_expired(self) -> None:
        now = self._now()
        expired = []
        for session_id, value in self._records.items():
            if float(value["expires_at"]) <= now:
                expired.append((session_id, value))
        for session_id, value in expired:
            self._records.pop(session_id, None)
            self._browser_index.pop(str(value["browser_capability_hash"]), None)
            self._agent_index.pop(str(value["agent_capability_hash"]), None)
        for bucket, (_count, expires_at) in tuple(self._quotas.items()):
            if expires_at <= now:
                self._quotas.pop(bucket, None)
        for bucket, (_count, expires_at) in tuple(self._rate_limits.items()):
            if expires_at <= now:
                self._rate_limits.pop(bucket, None)


class RedisClient(Protocol):
    """Subset shared by redis-py and Redis-compatible clients."""

    def get(self, key: str) -> bytes | str | None: ...

    def eval(self, script: str, numkeys: int, *keys_and_args: Any) -> Any: ...


class RedisRepository:
    """The Redis-compatible durable repository used by the remote service.

    No package-level redis dependency is required: settings inject the narrow
    client protocol.  The Lua scripts provide TTL and multi-key atomicity on
    Redis and compatible managed services.
    """

    _CREATE = """
local existing = redis.call('GET', KEYS[1])
if existing then return 0 end
if redis.call('GET', KEYS[2]) or redis.call('GET', KEYS[3]) then return 0 end
redis.call('SET', KEYS[1], ARGV[1], 'EX', ARGV[2])
redis.call('SET', KEYS[2], ARGV[3], 'EX', ARGV[2])
redis.call('SET', KEYS[3], ARGV[3], 'EX', ARGV[2])
return 1
"""
    _CAS = """
local raw = redis.call('GET', KEYS[1])
if not raw then return 0 end
local decoded = cjson.decode(raw)
if decoded.record_version ~= tonumber(ARGV[1]) then return 0 end
redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
redis.call('SET', KEYS[2], ARGV[4], 'EX', ARGV[3])
redis.call('SET', KEYS[3], ARGV[4], 'EX', ARGV[3])
return 1
"""
    _QUOTA = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current >= tonumber(ARGV[1]) then return 0 end
redis.call('INCR', KEYS[1])
if current == 0 then redis.call('EXPIRE', KEYS[1], ARGV[2]) end
return 1
"""

    def __init__(self, client: RedisClient, *, now: callable, prefix: str = "winnow:remote:v1") -> None:
        self._client = client
        self._now = now
        self._prefix = prefix.rstrip(":")

    def create(self, record: ActiveSession) -> None:
        ttl = self._ttl(record.expires_at)
        response = self._client.eval(
            self._CREATE,
            3,
            self._record_key(record.session_id),
            self._browser_key(record.browser_capability_hash),
            self._agent_key(record.agent_capability_hash),
            self._dump(record),
            ttl,
            record.session_id,
        )
        if int(response) != 1:
            raise RecordConflict("session or capability index already exists")

    def lookup_browser(self, capability_hash: str) -> StoredSession | None:
        return self._lookup_index(self._browser_key(capability_hash))

    def lookup_agent(self, capability_hash: str) -> StoredSession | None:
        return self._lookup_index(self._agent_key(capability_hash))

    def get(self, session_id: str) -> StoredSession | None:
        value = self._client.get(self._record_key(session_id))
        return self._load(value)

    def compare_and_swap(self, session_id: str, expected_record_version: int, replacement: StoredSession) -> bool:
        if replacement.session_id != session_id or replacement.record_version != expected_record_version + 1:
            raise RepositoryError("replacement record version is invalid")
        ttl = self._ttl(replacement.expires_at)
        response = self._client.eval(
            self._CAS,
            3,
            self._record_key(session_id),
            self._browser_key(replacement.browser_capability_hash),
            self._agent_key(replacement.agent_capability_hash),
            expected_record_version,
            self._dump(replacement),
            ttl,
            session_id,
        )
        return int(response) == 1

    def increment_quota(self, bucket: str, *, expires_at: float, limit: int) -> bool:
        ttl = self._ttl(expires_at)
        response = self._client.eval(self._QUOTA, 1, f"{self._prefix}:quota:{bucket}", limit, ttl)
        return int(response) == 1

    def increment_rate_limit(self, bucket: str, *, expires_at: float, limit: int) -> bool:
        ttl = self._ttl(expires_at)
        response = self._client.eval(self._QUOTA, 1, f"{self._prefix}:rate:{bucket}", limit, ttl)
        return int(response) == 1

    def _lookup_index(self, index_key: str) -> StoredSession | None:
        value = self._client.get(index_key)
        if value is None:
            return None
        session_id = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        return self.get(session_id)

    def _ttl(self, expires_at: float) -> int:
        # Redis EX requires a positive integer.  The coordinator may still turn
        # a record into a tombstone at the expiry edge before Redis reaps it.
        return max(1, int(expires_at - self._now()))

    @staticmethod
    def _dump(record: StoredSession) -> str:
        return json.dumps(record.as_dict(), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

    @staticmethod
    def _load(value: bytes | str | None) -> StoredSession | None:
        if value is None:
            return None
        try:
            raw = value.decode("utf-8") if isinstance(value, bytes) else value
            return record_from_dict(json.loads(raw))
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError, RepositoryError) as exc:
            raise RepositoryError("stored record is invalid") from exc

    def _record_key(self, session_id: str) -> str:
        return f"{self._prefix}:session:{session_id}"

    def _browser_key(self, capability_hash: str) -> str:
        return f"{self._prefix}:browser:{capability_hash}"

    def _agent_key(self, capability_hash: str) -> str:
        return f"{self._prefix}:agent:{capability_hash}"
