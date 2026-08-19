"""Browser HTTPS endpoints with exact capability and CORS boundaries."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .contracts import BrowserNextRoundRequest, ContractError
from .coordinator import AuthenticationError, Coordinator, CoordinatorError, StateConflict, canonical_preflight_origin
from .herenow import MAX_REMOTE_BROWSER_REQUEST_BYTES
from .settings import RateLimitError, RateLimiter, WaitNotifier


_HASH = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_REQUEST_HEADERS = frozenset({"accept", "authorization", "content-type"})
_CORS_METHODS = "GET, POST, OPTIONS"
_CORS_HEADERS = "Accept, Authorization, Content-Type"


class BrowserRequestError(ValueError):
    """A safe, generic malformed browser-request failure."""


def _positive_query(value: str | None) -> int:
    if value is None or not value.isascii() or not value.isdecimal():
        raise BrowserRequestError("invalid query")
    parsed = int(value)
    if parsed < 1:
        raise BrowserRequestError("invalid query")
    return parsed


def _single_query(request: Request, required: set[str]) -> dict[str, str]:
    pairs = list(request.query_params.multi_items())
    if {key for key, _value in pairs} != required or len(pairs) != len(required) or len({key for key, _value in pairs}) != len(pairs):
        raise BrowserRequestError("invalid query")
    return dict(pairs)


def _authorization(request: Request) -> str:
    value = request.headers.get("authorization")
    if not isinstance(value, str) or not value.startswith("Bearer "):
        raise BrowserRequestError("invalid authorization")
    capability = value[7:]
    if not capability or len(capability) > 512 or capability != capability.strip() or any(ord(item) < 33 or ord(item) == 127 for item in capability):
        raise BrowserRequestError("invalid authorization")
    return capability


def _origin(request: Request) -> str:
    value = request.headers.get("origin")
    if not isinstance(value, str) or not value or len(value) > 512:
        raise BrowserRequestError("invalid origin")
    return value


async def _bounded_json_body(request: Request) -> Any:
    content_type = request.headers.get("content-type", "")
    media_type = content_type.split(";", 1)[0].strip().lower()
    if media_type != "application/json":
        raise BrowserRequestError("invalid content type")
    declared = request.headers.get("content-length")
    if declared is not None:
        if not declared.isdecimal() or int(declared) > MAX_REMOTE_BROWSER_REQUEST_BYTES:
            raise BrowserRequestError("invalid body length")
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > MAX_REMOTE_BROWSER_REQUEST_BYTES:
            raise BrowserRequestError("invalid body length")
    if not body:
        raise BrowserRequestError("invalid JSON")

    def no_duplicate_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise BrowserRequestError("invalid JSON")
            result[key] = value
        return result

    try:
        return json.loads(bytes(body).decode("utf-8"), object_pairs_hook=no_duplicate_object)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
        raise BrowserRequestError("invalid JSON") from exc


@dataclass(frozen=True)
class BrowserApiConfig:
    allowed_herenow_host_suffixes: tuple[str, ...] = (".here.now",)
    preflight_max_age_seconds: int = 300

    def __post_init__(self) -> None:
        if self.preflight_max_age_seconds < 1 or self.preflight_max_age_seconds > 600:
            raise ValueError("preflight cache age is invalid")


class BrowserApi:
    """Routes are deliberately orchestration-only: contracts live below HTTP."""

    def __init__(self, coordinator: Coordinator, *, rate_limiter: RateLimiter, notifier: WaitNotifier, config: BrowserApiConfig = BrowserApiConfig()) -> None:
        self._coordinator = coordinator
        self._rate_limiter = rate_limiter
        self._notifier = notifier
        self._config = config

    def routes(self) -> list[Route]:
        return [
            Route("/v1/session/status", self.status, methods=["GET", "OPTIONS"]),
            Route("/v1/session/next-round", self.next_round, methods=["POST", "OPTIONS"]),
        ]

    async def status(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return self._preflight(request)
        cors_origin: str | None = None
        try:
            capability = _authorization(request)
            origin = _origin(request)
            cors_origin = self._coordinator.browser_cors_origin(capability, origin=origin)
            query = _single_query(request, {"roundNumber", "seedHash", "publishedRevision"})
            _positive_query(query["roundNumber"])
            if not _HASH.fullmatch(query["seedHash"]):
                raise BrowserRequestError("invalid query")
            revision = _positive_query(query["publishedRevision"])
            self._rate_limiter.status(capability)
            result = self._coordinator.browser_status(capability, origin=origin, embedded_revision=revision)
            return self._json(200, self._public_status(result), cors_origin)
        except AuthenticationError:
            return self._error(401, cors_origin)
        except (BrowserRequestError, ContractError, CoordinatorError, StateConflict, RateLimitError):
            return self._error(400, cors_origin)

    async def next_round(self, request: Request) -> Response:
        if request.method == "OPTIONS":
            return self._preflight(request)
        cors_origin: str | None = None
        try:
            capability = _authorization(request)
            origin = _origin(request)
            cors_origin = self._coordinator.browser_cors_origin(capability, origin=origin)
            self._rate_limiter.next_round(capability)
            raw = await _bounded_json_body(request)
            parsed = BrowserNextRoundRequest.parse(raw)
            result = self._coordinator.accept_browser_next_round(capability, origin=origin, request=parsed)
            if result.get("status") == "accepted":
                session_id = self._coordinator.browser_wait_notification_key(capability, origin=origin)
                if session_id is not None:
                    self._notifier.notify(session_id)
            return self._json(200, self._public_next_round(result), cors_origin)
        except AuthenticationError:
            return self._error(401, cors_origin)
        except (BrowserRequestError, ContractError, CoordinatorError, StateConflict, RateLimitError):
            return self._error(400, cors_origin)

    def _preflight(self, request: Request) -> Response:
        origin = request.headers.get("origin")
        method = request.headers.get("access-control-request-method", "").upper()
        requested_headers = {value.strip().lower() for value in request.headers.get("access-control-request-headers", "").split(",") if value.strip()}
        if (
            not isinstance(origin, str)
            or not canonical_preflight_origin(origin, allowed_host_suffixes=self._config.allowed_herenow_host_suffixes)
            or method not in {"GET", "POST"}
            or not requested_headers.issubset(_ALLOWED_REQUEST_HEADERS)
        ):
            return self._error(403)
        return Response(status_code=204, headers=self._cors_headers(origin, preflight=True))

    @staticmethod
    def _public_status(result: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"status", "roundNumber", "seedHash", "publishedRevision", "expiresAt", "agentLeaseExpiresAt", "remainingOptionCapacity"}
        return {key: result[key] for key in allowed if key in result}

    @staticmethod
    def _public_next_round(result: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"status", "roundNumber", "publishedRevision"}
        return {key: result[key] for key in allowed if key in result}

    def _json(self, status: int, value: Mapping[str, Any], origin: str) -> JSONResponse:
        return JSONResponse(dict(value), status_code=status, headers=self._cors_headers(origin))

    def _error(self, status: int, origin: str | None = None) -> JSONResponse:
        # Do not echo a malformed URL, Origin, body, bearer, provider failure,
        # or internal session state.  No CORS header is safe without exact
        # active/tombstone authorization.
        headers = {"Cache-Control": "no-store", "Content-Type": "application/json; charset=utf-8"}
        if origin is not None:
            headers.update(self._cors_headers(origin))
        return JSONResponse({"error": "request_rejected"}, status_code=status, headers=headers)

    def _cors_headers(self, origin: str, *, preflight: bool = False) -> dict[str, str]:
        headers = {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": _CORS_METHODS,
            "Access-Control-Allow-Headers": _CORS_HEADERS,
            "Access-Control-Max-Age": str(self._config.preflight_max_age_seconds),
            "Cache-Control": "no-store",
            "Vary": "Origin, Access-Control-Request-Method, Access-Control-Request-Headers",
        }
        if not preflight:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return headers
