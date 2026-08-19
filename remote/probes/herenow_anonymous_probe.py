#!/usr/bin/env python3
"""Disposable Work Package 0 HereNow anonymous create/update probe.

The script creates one generic, non-sensitive anonymous page, updates its same
slug once, reports only redacted/measured metadata, and discards the returned
claim token in process memory.  HereNow documents no anonymous delete endpoint;
the created page is therefore intentionally generic and left to its documented
24-hour expiry.  This is evidence-gathering code, not a publisher adapter.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


API = "https://here.now/api/v1/publish"
CONTENT_TYPE = "text/html; charset=utf-8"
TARGET_BYTES = 131_072
MAX_TEST_FILE_BYTES = 1_048_576
MAX_REQUEST_TIMEOUT_SECONDS = 30.0
MAX_CONVERGENCE_WINDOW_SECONDS = 30.0


def request_json(url: str, method: str, body: dict[str, Any], timeout_seconds: float) -> tuple[int, dict[str, Any], dict[str, str]]:
    request = urllib.request.Request(url, data=json.dumps(body, separators=(",", ":")).encode("utf-8"), headers={"Accept": "application/json", "Content-Type": "application/json", "X-HereNow-Client": "winnow-wp0-probe/0"}, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return response.status, json.loads(response.read().decode("utf-8")), selected_headers(response.headers)
    except urllib.error.HTTPError as error:
        raw = error.read()
        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = {}
        return error.code, data, selected_headers(error.headers)


def selected_headers(headers: Any) -> dict[str, str]:
    allowed = ("cache-control", "age", "etag", "last-modified", "x-cache", "cf-cache-status", "retry-after", "ratelimit-limit", "ratelimit-remaining", "ratelimit-reset", "x-ratelimit-limit", "x-ratelimit-remaining", "x-ratelimit-reset")
    return {name: str(headers[name]) for name in allowed if headers.get(name) is not None}


def probe_html(marker: str, target_bytes: int) -> bytes:
    prefix = f"<!doctype html><meta charset=utf-8><meta name=wp0-marker content={marker}><title>Winnow WP0 disposable probe</title><main>Disposable non-sensitive HereNow probe.</main>".encode("utf-8")
    return prefix + b" " * max(0, target_bytes - len(prefix))


def upload(url: str, headers: dict[str, Any], html: bytes, timeout_seconds: float) -> tuple[int, dict[str, str]]:
    upload_headers = {str(key): str(value) for key, value in headers.items() if str(key).lower() != "authorization"}
    upload_headers.setdefault("Content-Type", CONTENT_TYPE)
    request = urllib.request.Request(url, data=html, headers=upload_headers, method="PUT")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        response.read()
        return response.status, selected_headers(response.headers)


def page_marker(site_url: str, timeout_seconds: float) -> tuple[int, bool, dict[str, str]]:
    request = urllib.request.Request(site_url, headers={"Accept": "text/html", "Cache-Control": "no-cache", "User-Agent": "Mozilla/5.0 (compatible; Winnow-WP0-Probe/0)"}, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            page = response.read(2_000_000)
            return response.status, b"wp0-marker" in page and b"revision-2" in page, selected_headers(response.headers)
    except urllib.error.HTTPError as error:
        error.read()
        return error.code, False, selected_headers(error.headers)


def response_expiry(value: dict[str, Any]) -> str | None:
    direct = value.get("expiresAt")
    if isinstance(direct, str):
        return direct
    publish_status = value.get("publishStatus")
    if isinstance(publish_status, dict) and isinstance(publish_status.get("expiresAt"), str):
        return publish_status["expiresAt"]
    return None


@dataclass(frozen=True)
class ProbeResult:
    invalid_create_status: int
    invalid_create_response_keys: list[str]
    create_status: int
    create_response_keys: list[str]
    claim_token_length: int
    claim_token_character_class: str
    upload_statuses: list[int]
    finalize_statuses: list[int]
    update_status: int
    update_response_keys: list[str]
    original_expiry_reported_by_update: bool
    original_expiry_preserved: bool | None
    original_expiry_seconds_at_create: int
    finalize_expiry_reported: list[bool]
    finalize_expiry_preserved: list[bool | None]
    finalize_retry_status: int
    finalize_retry_response_keys: list[str]
    finalize_retry_replayed: bool | None
    update_converged: bool
    update_convergence_milliseconds: int
    page_statuses_after_update: list[int]
    page_headers_after_update: dict[str, str]
    observed_rate_limit_headers: dict[str, dict[str, str]]
    tested_file_bytes: int


def run(request_timeout_seconds: float = MAX_REQUEST_TIMEOUT_SECONDS, convergence_window_seconds: float = MAX_CONVERGENCE_WINDOW_SECONDS, poll_interval_seconds: float = 1.0, target_bytes: int = TARGET_BYTES) -> ProbeResult:
    if not 0 < request_timeout_seconds <= MAX_REQUEST_TIMEOUT_SECONDS:
        raise ValueError(f"request timeout must be in (0, {MAX_REQUEST_TIMEOUT_SECONDS}]")
    if not 0 < convergence_window_seconds <= MAX_CONVERGENCE_WINDOW_SECONDS:
        raise ValueError(f"convergence window must be in (0, {MAX_CONVERGENCE_WINDOW_SECONDS}]")
    if not 0 < poll_interval_seconds <= 5:
        raise ValueError("poll interval must be in (0, 5]")
    if not 1 <= target_bytes <= MAX_TEST_FILE_BYTES:
        raise ValueError(f"target bytes must be in [1, {MAX_TEST_FILE_BYTES}]")
    invalid_status, invalid_response, invalid_headers = request_json(API, "POST", {"files": []}, request_timeout_seconds)
    html_one = probe_html("revision-1", target_bytes)
    manifest = {"files": [{"path": "index.html", "size": len(html_one), "contentType": CONTENT_TYPE, "hash": hashlib.sha256(html_one).hexdigest()}]}
    create_status, created, create_headers = request_json(API, "POST", manifest, request_timeout_seconds)
    required = ("slug", "siteUrl", "expiresAt", "claimToken", "anonymous", "upload")
    if create_status // 100 != 2 or any(key not in created for key in required) or created.get("anonymous") is not True:
        raise RuntimeError(f"anonymous create failed with HTTP {create_status}; returned fields: {sorted(created)}")
    claim_token = created["claimToken"]
    if not isinstance(claim_token, str):
        raise RuntimeError("anonymous create returned a non-string claim token")
    initial_expiry = created["expiresAt"]
    if not isinstance(initial_expiry, str):
        raise RuntimeError("anonymous create returned a non-string expiration")
    expires_at = dt.datetime.fromisoformat(initial_expiry.replace("Z", "+00:00"))
    expiry_seconds_at_create = int((expires_at - dt.datetime.now(dt.timezone.utc)).total_seconds())
    created_keys = sorted(created)
    claim_token_length = len(claim_token)
    upload_one = created["upload"]
    first_upload = next(item for item in upload_one["uploads"] if item.get("path") == "index.html")
    upload_status_one, upload_headers_one = upload(first_upload["url"], first_upload.get("headers", {}), html_one, request_timeout_seconds)
    finalize_status_one, finalize_one, finalize_headers_one = request_json(upload_one["finalizeUrl"], "POST", {"versionId": upload_one["versionId"]}, request_timeout_seconds)
    slug = created["slug"]
    site_url = created["siteUrl"]
    html_two = probe_html("revision-2", target_bytes)
    update_manifest = {"files": [{"path": "index.html", "size": len(html_two), "contentType": CONTENT_TYPE, "hash": hashlib.sha256(html_two).hexdigest()}], "claimToken": claim_token}
    update_status, updated, update_headers = request_json(f"{API}/{slug}", "PUT", update_manifest, request_timeout_seconds)
    if update_status // 100 != 2:
        raise RuntimeError(f"anonymous update failed with HTTP {update_status}; returned fields: {sorted(updated)}")
    upload_two = updated.get("upload")
    if not isinstance(upload_two, dict):
        raise RuntimeError("anonymous update did not return upload metadata")
    second_upload = next(item for item in upload_two["uploads"] if item.get("path") == "index.html")
    upload_status_two, upload_headers_two = upload(second_upload["url"], second_upload.get("headers", {}), html_two, request_timeout_seconds)
    finalize_status_two, finalize_two, finalize_headers_two = request_json(upload_two["finalizeUrl"], "POST", {"versionId": upload_two["versionId"]}, request_timeout_seconds)
    finalize_retry_status, finalize_retry, finalize_retry_headers = request_json(upload_two["finalizeUrl"], "POST", {"versionId": upload_two["versionId"]}, request_timeout_seconds)
    updated_keys = sorted(updated)
    update_expiry = updated.get("expiresAt")
    finalize_expiries = [response_expiry(finalize_one), response_expiry(finalize_two), response_expiry(finalize_retry)]
    started = time.monotonic()
    page_headers: dict[str, str] = {}
    page_statuses: list[int] = []
    converged = False
    while time.monotonic() - started <= convergence_window_seconds:
        status, marker_matches, page_headers = page_marker(site_url, request_timeout_seconds)
        page_statuses.append(status)
        if marker_matches:
            converged = True
            break
        time.sleep(poll_interval_seconds)
    token_class = "url-safe-like" if claim_token.replace("-", "").replace("_", "").isalnum() else "opaque-non-url-safe"
    # Delete references before returning; output contains no site URL, slug,
    # upload URLs, finalizer URL, claim token, or response bodies.
    del claim_token, created, updated, finalize_one, finalize_two, site_url, slug
    return ProbeResult(
        invalid_create_status=invalid_status,
        invalid_create_response_keys=sorted(invalid_response),
        create_status=create_status,
        create_response_keys=created_keys,
        claim_token_length=claim_token_length,
        claim_token_character_class=token_class,
        upload_statuses=[upload_status_one, upload_status_two],
        finalize_statuses=[finalize_status_one, finalize_status_two],
        update_status=update_status,
        update_response_keys=updated_keys,
        original_expiry_reported_by_update=isinstance(update_expiry, str),
        original_expiry_preserved=update_expiry == initial_expiry if isinstance(update_expiry, str) else None,
        original_expiry_seconds_at_create=expiry_seconds_at_create,
        finalize_expiry_reported=[value is not None for value in finalize_expiries],
        finalize_expiry_preserved=[value == initial_expiry if value is not None else None for value in finalize_expiries],
        finalize_retry_status=finalize_retry_status,
        finalize_retry_response_keys=sorted(finalize_retry),
        finalize_retry_replayed=finalize_retry.get("replayed") if isinstance(finalize_retry.get("replayed"), bool) else None,
        update_converged=converged,
        update_convergence_milliseconds=int((time.monotonic() - started) * 1000),
        page_statuses_after_update=page_statuses,
        page_headers_after_update=page_headers,
        observed_rate_limit_headers={"invalidCreate": invalid_headers, "create": create_headers, "upload1": upload_headers_one, "finalize1": finalize_headers_one, "update": update_headers, "upload2": upload_headers_two, "finalize2": finalize_headers_two, "finalizeRetry": finalize_retry_headers},
        tested_file_bytes=len(html_two),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Disposable WP0 HereNow anonymous create/update probe")
    parser.add_argument("--request-timeout-seconds", type=float, default=MAX_REQUEST_TIMEOUT_SECONDS, help=f"per-request timeout from 0 to {MAX_REQUEST_TIMEOUT_SECONDS} seconds")
    parser.add_argument("--convergence-window-seconds", type=float, default=MAX_CONVERGENCE_WINDOW_SECONDS, help=f"marker polling window from 0 to {MAX_CONVERGENCE_WINDOW_SECONDS} seconds")
    parser.add_argument("--poll-interval-seconds", type=float, default=1.0, help="marker polling interval from 0 to 5 seconds")
    parser.add_argument("--target-bytes", type=int, default=TARGET_BYTES, help=f"generic index.html size from 1 to {MAX_TEST_FILE_BYTES} bytes")
    arguments = parser.parse_args()
    print(json.dumps(run(arguments.request_timeout_seconds, arguments.convergence_window_seconds, arguments.poll_interval_seconds, arguments.target_bytes).__dict__, separators=(",", ":"), sort_keys=True))
