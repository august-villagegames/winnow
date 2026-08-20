"""Internal HereNow create/update adapter for the remote Winnow service.

There is intentionally no MCP or browser code here.  This module owns the
provider ordering and secret boundary that later coordinator code will call.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .security import HostedImageFetcher, verify_remote_current_images


# WP0 positively exercised a generic 1 MiB page, while provider documentation
# described a much larger limit.  Keep this remote v1 budget conservative until
# a rolling 100-option artifact is measured in the deployed host transport.
HERENOW_WP0_VERIFIED_HTML_BYTES = 1_048_576
MAX_REMOTE_COMPILED_HTML_BYTES = HERENOW_WP0_VERIFIED_HTML_BYTES
MAX_REMOTE_CREATE_SEED_BYTES = 262_144
MAX_REMOTE_SUCCESSOR_SEED_BYTES = 786_432
MAX_REMOTE_BROWSER_REQUEST_BYTES = 32_768
MAX_REMOTE_STORED_RECORD_BYTES = 2_097_152
MAX_REMOTE_MCP_RESULT_BYTES = 32_768
# A ``continue_requested`` result also carries fixed agent instructions and
# fenced publication arguments around the continuation itself.  Reserve room
# for that assistant-readable handoff instead of allowing a continuation to
# consume the entire MCP result budget on its own.
MAX_REMOTE_CONTINUATION_HANDOFF_BYTES = 30_000
# The outer JSON-RPC envelope is bounded independently of either seed.  It
# leaves modest room around the largest supported successor while preventing
# the ASGI transport from accepting an arbitrarily large body before the MCP
# SDK decodes it.
MAX_REMOTE_MCP_REQUEST_BYTES = 1_048_576
MAX_LIVE_MARKER_HTML_BYTES = 2_000_000
LIVE_VERIFY_DELAYS_SECONDS = (0.0, 5.0, 15.0, 30.0)

HERE_NOW_PUBLISH_ENDPOINT = "https://here.now/api/v1/publish"
HERE_NOW_CONTENT_TYPE = "text/html; charset=utf-8"


class HereNowError(RuntimeError):
    """A public-safe provider failure without a response body or secret."""


def _load_portable_core() -> Any:
    existing = sys.modules.get("winnow_portable_core")
    if existing is not None:
        return existing
    root = Path(__file__).resolve().parents[3]
    path = root / ".agents" / "skills" / "winnow" / "scripts" / "winnow.py"
    spec = importlib.util.spec_from_file_location("winnow_portable_core", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("portable Winnow publisher is unavailable")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _response_expiration(value: Mapping[str, Any]) -> str | None:
    direct = value.get("expiresAt")
    if isinstance(direct, str):
        return direct
    status = value.get("publishStatus")
    if isinstance(status, Mapping) and isinstance(status.get("expiresAt"), str):
        return status["expiresAt"]
    return None


def _normalized_expiration_marker(expires_at: str) -> str:
    """Match the portable compiler's fixed-width UTC expiration marker."""

    try:
        value = dt.datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as exc:
        raise HereNowError("HereNow expiration is invalid") from exc
    return (
        value.astimezone(dt.timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


@dataclass(frozen=True)
class PendingVersion:
    """Restart-safe provider state that must be persisted before upload."""

    slug: str
    site_url: str
    original_expires_at: str
    version_id: str
    published_revision: int
    expected_markers: Mapping[str, tuple[str, str]]
    upload_url: str = field(repr=False)
    upload_headers: Mapping[str, Any] = field(repr=False)
    finalize_url: str = field(repr=False)


@dataclass(frozen=True)
class InternalPublication:
    """Server-only result.  Do not serialize or return through MCP."""

    created: Any = field(repr=False)
    pending_version: PendingVersion
    image_verification: Mapping[str, Any]
    reconciled_after_ambiguous_finalize: bool


@dataclass(frozen=True)
class InternalUpdatedSite:
    """Internal update identity, deliberately without the claim token."""

    slug: str
    site_url: str
    expires_at: str


@dataclass(frozen=True)
class PublicRemotePublicationReceipt:
    """Explicit allowlist for a future MCP create/publish result."""

    site_url: str
    expires_at: str
    session_id: str
    seed_hash: str
    round_number: int
    published_revision: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "siteUrl": self.site_url,
            "expiresAt": self.expires_at,
            "sessionId": self.session_id,
            "seedHash": self.seed_hash,
            "roundNumber": self.round_number,
            "publishedRevision": self.published_revision,
        }


def expected_live_markers(
    *,
    session_id: str,
    seed_hash: str,
    runtime_version: str,
    expires_at: str,
    rolling_version: int | None = None,
    published_revision: int | None = None,
) -> dict[str, tuple[str, str]]:
    """Build the only marker set accepted for a publication revision."""

    normalized_expiration = _normalized_expiration_marker(expires_at)
    markers = {
        "session id": ("winnow-session-id", session_id),
        "seed hash": ("winnow-seed-hash", seed_hash),
        "runtime version": ("winnow-runtime-version", runtime_version),
        # The portable compiler normalizes provider timestamps to UTC with
        # millisecond precision before inserting this marker. HereNow may omit
        # fractional seconds, so compare the compiler's canonical form.
        "expiration": ("winnow-expires-at", normalized_expiration),
    }
    if rolling_version is not None:
        markers["rolling version"] = ("winnow-rolling-version", str(rolling_version))
    if published_revision is not None:
        markers["published revision"] = ("winnow-published-revision", str(published_revision))
    return markers


class HereNowPublisher:
    """Create and update a HereNow site without leaking its claim token.

    ``persist_claim`` and ``persist_pending`` are required callbacks.  The
    future coordinator supplies atomic encrypted persistence; a failed callback
    aborts before any upload/finalize side effect.
    """

    def __init__(
        self,
        *,
        core: Any | None = None,
        endpoint: str = HERE_NOW_PUBLISH_ENDPOINT,
        image_fetcher: HostedImageFetcher | None = None,
        image_verifier: Callable[[Mapping[str, Any]], Mapping[str, Any]] | None = None,
        request_json: Callable[..., tuple[int, dict[str, Any]]] | None = None,
        upload: Callable[..., None] | None = None,
        marker_verifier: Callable[[str, Mapping[str, tuple[str, str]]], None] | None = None,
        html_builder: Callable[[Mapping[str, Any], str | None], bytes] | None = None,
        retry_delays_seconds: Sequence[float] = LIVE_VERIFY_DELAYS_SECONDS,
        retry_delay: Callable[[float], None] = time.sleep,
    ) -> None:
        self._core = core or _load_portable_core()
        self._endpoint = endpoint
        self._request_json = request_json or self._core._http_json
        self._upload = upload or self._core._http_upload
        self._marker_verifier = marker_verifier or self._default_marker_verifier
        self._html_builder = html_builder
        self._retry_delays = tuple(retry_delays_seconds)
        self._retry_delay = retry_delay
        if image_verifier is not None:
            self._image_verifier = image_verifier
        else:
            fetcher = image_fetcher or HostedImageFetcher()
            self._image_verifier = lambda seed: verify_remote_current_images(seed, fetcher)

    def create(
        self,
        seed: Mapping[str, Any],
        *,
        persist_claim: Callable[[Any], None],
        published_revision: int,
        expected_markers: Mapping[str, tuple[str, str]] | Callable[[str], Mapping[str, tuple[str, str]]],
    ) -> InternalPublication:
        self._validate_seed(seed, MAX_REMOTE_CREATE_SEED_BYTES)
        image_verification = self._image_verifier(seed)
        provisional_html = self._build_html(seed)
        self._assert_html_size(provisional_html)
        try:
            created = self._core.create_anonymous_site_internal(
                provisional_html,
                endpoint=self._endpoint,
                require_claim_token=True,
                request_json=self._request_json,
                client_header="winnow-remote/1",
            )
        except HereNowError:
            raise
        except Exception:
            raise HereNowError("HereNow create failed") from None
        if not created.slug or not created.claim_token:
            raise HereNowError("HereNow create response is missing required internal metadata")
        self._persist_or_abort(persist_claim, created, "publication secret")
        final_html = self._build_html(seed, expires_at=created.expires_at)
        self._assert_final_html_size(provisional_html, final_html)
        markers = self._resolve_expiration_markers(expected_markers, created.expires_at)
        pending = PendingVersion(
            slug=created.slug,
            site_url=created.site_url,
            original_expires_at=created.expires_at,
            version_id=created.version_id,
            published_revision=published_revision,
            expected_markers=markers,
            upload_url=created.upload_url,
            upload_headers=dict(created.upload_headers),
            finalize_url=created.finalize_url,
        )
        reconciled = self._upload_finalize_and_verify(pending, final_html)
        return InternalPublication(created=created, pending_version=pending, image_verification=image_verification, reconciled_after_ambiguous_finalize=reconciled)

    def update(
        self,
        seed: Mapping[str, Any],
        *,
        slug: str,
        claim_token: str,
        site_url: str,
        original_expires_at: str,
        persist_pending: Callable[[PendingVersion], None],
        published_revision: int,
        expected_markers: Mapping[str, tuple[str, str]],
    ) -> InternalPublication:
        self._validate_seed(seed, MAX_REMOTE_SUCCESSOR_SEED_BYTES)
        if not isinstance(slug, str) or not slug or not isinstance(claim_token, str) or not claim_token:
            raise HereNowError("HereNow update is missing internal site metadata")
        image_verification = self._image_verifier(seed)
        html = self._build_html(seed, expires_at=original_expires_at)
        self._assert_html_size(html)
        try:
            status, response = self._request_json(
                f"{self._endpoint}/{urllib_quote_slug(slug)}",
                "PUT",
                {"files": [{"path": "index.html", "size": len(html), "contentType": HERE_NOW_CONTENT_TYPE}], "claimToken": claim_token},
                headers={"X-HereNow-Client": "winnow-remote/1"},
            )
        except Exception:
            raise HereNowError("HereNow update failed") from None
        if status < 200 or status >= 300:
            raise HereNowError("HereNow update was not accepted")
        returned_expiry = _response_expiration(response)
        if returned_expiry is not None and returned_expiry != original_expires_at:
            raise HereNowError("HereNow update changed the original expiration")
        markers = self._resolve_expiration_markers(expected_markers, original_expires_at)
        pending = self._pending_from_update_response(
            response,
            slug=slug,
            site_url=site_url,
            original_expires_at=original_expires_at,
            published_revision=published_revision,
            expected_markers=markers,
        )
        self._persist_or_abort(persist_pending, pending, "pending publication")
        reconciled = self._upload_finalize_and_verify(pending, html)
        # Claim material remains in the caller's encrypted state only.  This
        # result intentionally has no claim token and cannot be a public receipt.
        created = InternalUpdatedSite(slug=slug, site_url=site_url, expires_at=original_expires_at)
        return InternalPublication(created=created, pending_version=pending, image_verification=image_verification, reconciled_after_ambiguous_finalize=reconciled)

    def public_receipt(self, publication: InternalPublication, seed: Mapping[str, Any]) -> PublicRemotePublicationReceipt:
        """Construct an allowlisted receipt; never convert internal state directly."""

        return PublicRemotePublicationReceipt(
            site_url=publication.pending_version.site_url,
            expires_at=publication.pending_version.original_expires_at,
            session_id=str(seed["session"]["id"]),
            seed_hash=self._core.seed_hash(dict(seed)),
            round_number=int(seed["round"]["number"]),
            published_revision=publication.pending_version.published_revision,
        )

    def reconcile_pending(self, pending: PendingVersion) -> bool:
        """Only inspect live markers; never reissue an older provider update."""

        return self._verify_with_retries(pending)

    def _validate_seed(self, seed: Mapping[str, Any], max_bytes: int) -> None:
        try:
            encoded = self._core.canonical_json(dict(seed))
        except (TypeError, ValueError) as exc:
            raise HereNowError("remote seed cannot be serialized") from exc
        if len(encoded) > max_bytes:
            raise HereNowError("remote seed exceeds the byte limit")
        try:
            self._core.validate_seed(dict(seed))
        except Exception as exc:
            # The portable validator returns bounded, path-safe errors.  Keep
            # its public validation semantics without exposing provider data.
            raise HereNowError(str(exc)) from None

    def _build_html(self, seed: Mapping[str, Any], *, expires_at: str | None = None) -> bytes:
        try:
            if self._html_builder is not None:
                return self._html_builder(seed, expires_at)
            return self._core.build_html(dict(seed), expires_at=expires_at)
        except Exception as exc:
            raise HereNowError("remote HTML compilation failed") from exc

    @staticmethod
    def _assert_html_size(html: bytes) -> None:
        if len(html) > MAX_REMOTE_COMPILED_HTML_BYTES:
            raise HereNowError("compiled HTML exceeds the remote byte limit")

    def _assert_final_html_size(self, provisional_html: bytes, final_html: bytes) -> None:
        self._assert_html_size(final_html)
        if len(provisional_html) != len(final_html):
            raise HereNowError("provisional and final HTML byte lengths differ")

    @staticmethod
    def _persist_or_abort(callback: Callable[[Any], None], value: Any, label: str) -> None:
        try:
            callback(value)
        except Exception as exc:
            # A callback exception can contain database diagnostics or the
            # supplied claim token.  Do not chain or echo it.
            raise HereNowError(f"unable to persist {label}") from None

    @staticmethod
    def _resolve_expiration_markers(
        markers_or_factory: Mapping[str, tuple[str, str]] | Callable[[str], Mapping[str, tuple[str, str]]],
        original_expires_at: str,
    ) -> dict[str, tuple[str, str]]:
        markers = markers_or_factory(original_expires_at) if callable(markers_or_factory) else markers_or_factory
        normalized = dict(markers)
        if normalized.get("expiration") != ("winnow-expires-at", _normalized_expiration_marker(original_expires_at)):
            raise HereNowError("publication markers do not preserve the original expiration")
        return normalized

    def _pending_from_update_response(
        self,
        response: Mapping[str, Any],
        *,
        slug: str,
        site_url: str,
        original_expires_at: str,
        published_revision: int,
        expected_markers: Mapping[str, tuple[str, str]],
    ) -> PendingVersion:
        upload = response.get("upload")
        if not isinstance(upload, Mapping):
            raise HereNowError("HereNow update response is missing upload metadata")
        uploads = upload.get("uploads")
        if not isinstance(uploads, list):
            raise HereNowError("HereNow update response is missing upload metadata")
        matching = next((item for item in uploads if isinstance(item, Mapping) and item.get("path") == "index.html"), None)
        finalize_url = upload.get("finalizeUrl")
        version_id = upload.get("versionId")
        if matching is None or not isinstance(matching.get("url"), str) or not isinstance(finalize_url, str) or not isinstance(version_id, str):
            raise HereNowError("HereNow update response is missing upload metadata")
        headers = matching.get("headers")
        if headers is not None and not isinstance(headers, Mapping):
            raise HereNowError("HereNow update response has invalid upload headers")
        return PendingVersion(
            slug=slug,
            site_url=site_url,
            original_expires_at=original_expires_at,
            version_id=version_id,
            published_revision=published_revision,
            expected_markers=dict(expected_markers),
            upload_url=matching["url"],
            upload_headers=dict(headers or {}),
            finalize_url=finalize_url,
        )

    def _upload_finalize_and_verify(self, pending: PendingVersion, html: bytes) -> bool:
        try:
            self._upload(pending.upload_url, html, dict(pending.upload_headers))
            status, finalized = self._request_json(pending.finalize_url, "POST", {"versionId": pending.version_id})
            if status < 200 or status >= 300:
                raise HereNowError("HereNow finalization was not accepted")
            returned_expiry = _response_expiration(finalized)
            if returned_expiry is not None and returned_expiry != pending.original_expires_at:
                raise HereNowError("HereNow finalization changed the original expiration")
        except HereNowError:
            raise
        except Exception:
            # A transport response can be lost after HereNow commits.  Reconcile
            # markers before declaring failure; never submit an old update again.
            if self._verify_with_retries(pending):
                return True
            raise HereNowError("HereNow finalization could not be confirmed") from None
        if not self._verify_with_retries(pending):
            raise HereNowError("HereNow publication markers could not be verified")
        return False

    def _verify_with_retries(self, pending: PendingVersion) -> bool:
        for index, delay in enumerate(self._retry_delays):
            if index and delay > 0:
                self._retry_delay(delay)
            try:
                self._marker_verifier(pending.site_url, pending.expected_markers)
                return True
            except Exception:
                continue
        return False

    def _default_marker_verifier(self, site_url: str, markers: Mapping[str, tuple[str, str]]) -> None:
        self._core.fetch_live_markers(site_url, dict(markers), max_bytes=MAX_LIVE_MARKER_HTML_BYTES)


def urllib_quote_slug(slug: str) -> str:
    """Quote the one provider path segment without ever treating it as a URL."""

    from urllib.parse import quote

    return quote(slug, safe="")
