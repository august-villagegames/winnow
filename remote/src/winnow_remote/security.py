"""Remote-only SSRF-safe hosted image verification.

The portable CLI deliberately retains its existing permissive verifier.  A
hosted anonymous service needs a stricter network boundary, so this module is
only imported by remote publication code.
"""

from __future__ import annotations

import concurrent.futures
import base64
import hashlib
import hmac
import http.client
import ipaddress
import secrets
import socket
import ssl
import urllib.parse
import re
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Mapping, Protocol

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


REMOTE_IMAGE_MAX_BYTES = 8 * 1024 * 1024
REMOTE_IMAGE_MAX_REDIRECTS = 3
REMOTE_IMAGE_CONNECT_TIMEOUT_SECONDS = 5.0
REMOTE_IMAGE_READ_TIMEOUT_SECONDS = 10.0
REMOTE_IMAGE_MAX_CONCURRENCY = 4
REMOTE_IMAGE_CONTENT_TYPES = frozenset({"image/png", "image/jpeg", "image/gif", "image/webp", "image/avif"})
_BLOCKED_DESTINATION_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.0.0.0/24"),
    ipaddress.ip_network("192.0.2.0/24"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("198.51.100.0/24"),
    ipaddress.ip_network("203.0.113.0/24"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::/128"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("100::/64"),
    ipaddress.ip_network("2001:db8::/32"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("ff00::/8"),
)


class SecretError(ValueError):
    """A public-safe failure for malformed or undecryptable encrypted state."""


def _b64url_encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64url_decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or len(value) > 8192 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise SecretError("encrypted secret is invalid")
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SecretError("encrypted secret is invalid") from exc


@dataclass(frozen=True)
class EncryptedSecret:
    """AEAD ciphertext that is deliberately redacted by ``repr`` and logs."""

    key_id: str
    ciphertext: str

    def __repr__(self) -> str:
        return f"EncryptedSecret(key_id={self.key_id!r}, ciphertext=<redacted>)"

    def as_dict(self) -> dict[str, str]:
        return {"keyId": self.key_id, "ciphertext": self.ciphertext}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "EncryptedSecret":
        if set(value) != {"keyId", "ciphertext"} or not isinstance(value.get("keyId"), str) or not value["keyId"]:
            raise SecretError("encrypted secret is invalid")
        _b64url_decode(value.get("ciphertext"))
        return cls(key_id=value["keyId"], ciphertext=value["ciphertext"])


class CapabilitySecurity:
    """Creates bearer capabilities and protects provider claims at rest.

    Capability hashes use an independent keyed HMAC rather than a bare SHA-256
    digest.  Claim-token encryption binds every ciphertext to the internal
    session identity and key id, preventing record swapping across sessions or
    rotations.  Key rotation is an operator responsibility: provide every key
    still needed by active records in ``aead_keys``.
    """

    def __init__(self, *, capability_hmac_key: bytes, active_key_id: str, aead_keys: Mapping[str, bytes]) -> None:
        if not isinstance(capability_hmac_key, bytes) or len(capability_hmac_key) < 32:
            raise ValueError("capability HMAC key must contain at least 256 bits")
        if not isinstance(active_key_id, str) or not active_key_id or active_key_id not in aead_keys:
            raise ValueError("active AEAD key id is unavailable")
        normalized: dict[str, bytes] = {}
        for key_id, key in aead_keys.items():
            if not isinstance(key_id, str) or not key_id or not isinstance(key, bytes) or len(key) not in {16, 24, 32}:
                raise ValueError("AEAD keys must be AES-GCM keys")
            normalized[key_id] = key
        self._capability_hmac_key = capability_hmac_key
        self._active_key_id = active_key_id
        self._aead_keys = normalized

    @classmethod
    def ephemeral_for_tests(cls) -> "CapabilitySecurity":
        """Build isolated keys for deterministic domain tests, never settings."""

        return cls(capability_hmac_key=secrets.token_bytes(32), active_key_id="test", aead_keys={"test": secrets.token_bytes(32)})

    @staticmethod
    def new_capability() -> str:
        # token_urlsafe(32) exposes exactly 256 random bits and has no prefix
        # that could let a caller cross-use a browser and agent capability.
        return secrets.token_urlsafe(32)

    def browser_capability_for_session(self, session_id: str) -> str:
        """Derive, but never persist, the public browser bearer.

        A rolling successor must embed the same bearer after a process restart.
        Derivation from the server-only HMAC key and opaque session ID preserves
        the "hashes only" record invariant while retaining 256-bit output.
        The domain separates this label from every other HMAC use, so it cannot
        collide with a capability hash or quota digest.
        """

        if not isinstance(session_id, str) or not session_id or len(session_id) > 512:
            raise SecretError("session identity is invalid")
        material = hmac.new(
            self._capability_hmac_key,
            b"winnow-remote/browser-capability/v1\\0" + session_id.encode("utf-8"),
            hashlib.sha256,
        ).digest()
        return _b64url_encode(material)

    def capability_hash(self, capability: str) -> str:
        if not isinstance(capability, str) or not capability or len(capability) > 512:
            raise SecretError("capability is invalid")
        return hmac.new(self._capability_hmac_key, capability.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify_capability(self, capability: str, expected_hash: str) -> bool:
        try:
            actual = self.capability_hash(capability)
        except SecretError:
            return False
        return hmac.compare_digest(actual, expected_hash)

    def encrypt_claim_token(self, *, session_id: str, claim_token: str) -> EncryptedSecret:
        if not isinstance(session_id, str) or not session_id or not isinstance(claim_token, str) or not claim_token:
            raise SecretError("claim token is invalid")
        nonce = secrets.token_bytes(12)
        key_id = self._active_key_id
        aad = self._associated_data(session_id, key_id)
        ciphertext = AESGCM(self._aead_keys[key_id]).encrypt(nonce, claim_token.encode("utf-8"), aad)
        return EncryptedSecret(key_id=key_id, ciphertext=_b64url_encode(nonce + ciphertext))

    def decrypt_claim_token(self, *, session_id: str, encrypted: EncryptedSecret) -> str:
        key = self._aead_keys.get(encrypted.key_id)
        if key is None:
            raise SecretError("encrypted secret key is unavailable")
        blob = _b64url_decode(encrypted.ciphertext)
        if len(blob) < 12 + 16:
            raise SecretError("encrypted secret is invalid")
        try:
            value = AESGCM(key).decrypt(blob[:12], blob[12:], self._associated_data(session_id, encrypted.key_id))
            return value.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise SecretError("encrypted secret cannot be decrypted") from exc

    @staticmethod
    def _associated_data(session_id: str, key_id: str) -> bytes:
        return f"winnow-remote/claim/v1/{session_id}/{key_id}".encode("utf-8")


class HostedImageFetchError(ValueError):
    """A path-based error whose text never includes the supplied URL."""

    def __init__(self, path: str, reason: str):
        self.path = path
        self.reason = reason
        super().__init__(f"{path}: {reason}")


@dataclass(frozen=True)
class HostedImagePolicy:
    max_bytes: int = REMOTE_IMAGE_MAX_BYTES
    max_redirects: int = REMOTE_IMAGE_MAX_REDIRECTS
    connect_timeout_seconds: float = REMOTE_IMAGE_CONNECT_TIMEOUT_SECONDS
    read_timeout_seconds: float = REMOTE_IMAGE_READ_TIMEOUT_SECONDS
    max_concurrency: int = REMOTE_IMAGE_MAX_CONCURRENCY


@dataclass(frozen=True)
class _Target:
    url: str
    host: str
    port: int
    request_target: str


@dataclass(frozen=True)
class TransportResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class PinnedTransport(Protocol):
    def fetch(self, target: _Target, resolved_ip: str, policy: HostedImagePolicy) -> TransportResponse: ...


Resolver = Callable[[str, int], Iterable[str]]


def _is_public_destination(value: str) -> bool:
    """Accept only globally routable destination addresses.

    ``is_global`` covers loopback, private, link-local, multicast, reserved,
    unspecified, documentation, and carrier-grade NAT ranges on supported
    Python versions.  The explicit CGNAT check protects older implementations
    where it is not classified as private.
    """

    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if any(address in network for network in _BLOCKED_DESTINATION_NETWORKS):
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return _is_public_destination(str(address.ipv4_mapped))
    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    ):
        return False
    return address.is_global


def system_resolver(host: str, port: int) -> tuple[str, ...]:
    try:
        answers = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise OSError("name resolution failed") from exc
    values: list[str] = []
    for _family, _socktype, _proto, _canonname, sockaddr in answers:
        address = sockaddr[0]
        if address not in values:
            values.append(address)
    return tuple(values)


class StdlibPinnedTransport:
    """HTTPS transport that connects to the checked IP while retaining SNI.

    This avoids handing a hostname back to a library that could resolve it
    again after policy validation.  TLS still validates the original hostname.
    """

    def __init__(self, ssl_context: ssl.SSLContext | None = None) -> None:
        self._ssl_context = ssl_context or ssl.create_default_context()

    def fetch(self, target: _Target, resolved_ip: str, policy: HostedImagePolicy) -> TransportResponse:
        raw_socket: socket.socket | None = None
        tls_socket: ssl.SSLSocket | None = None
        try:
            raw_socket = socket.create_connection((resolved_ip, target.port), timeout=policy.connect_timeout_seconds)
            tls_socket = self._ssl_context.wrap_socket(raw_socket, server_hostname=target.host)
            raw_socket = None  # ownership moved to tls_socket
            tls_socket.settimeout(policy.read_timeout_seconds)
            host_header = target.host if target.port == 443 else f"{target.host}:{target.port}"
            request = (
                f"GET {target.request_target} HTTP/1.1\r\n"
                f"Host: {host_header}\r\n"
                "Accept: image/png, image/jpeg, image/gif, image/webp, image/avif\r\n"
                "User-Agent: winnow-remote-image-verifier/1\r\n"
                "Connection: close\r\n\r\n"
            ).encode("ascii")
            tls_socket.sendall(request)
            response = http.client.HTTPResponse(tls_socket)
            response.begin()
            headers = {key.lower(): value for key, value in response.getheaders()}
            body = response.read(policy.max_bytes + 1)
            return TransportResponse(status=response.status, headers=headers, body=body)
        except TimeoutError as exc:
            raise TimeoutError("HTTPS image fetch timed out") from exc
        except (OSError, ssl.SSLError, http.client.HTTPException, UnicodeError) as exc:
            raise OSError("HTTPS image fetch failed") from exc
        finally:
            if tls_socket is not None:
                tls_socket.close()
            elif raw_socket is not None:
                raw_socket.close()


class HostedImageFetcher:
    """Validates every redirect hop and pins its checked DNS destination."""

    def __init__(
        self,
        *,
        resolver: Resolver = system_resolver,
        transport: PinnedTransport | None = None,
        policy: HostedImagePolicy = HostedImagePolicy(),
    ) -> None:
        self._resolver = resolver
        self._transport = transport or StdlibPinnedTransport()
        self._policy = policy

    @property
    def policy(self) -> HostedImagePolicy:
        return self._policy

    def fetch(self, url: str, *, path: str) -> dict[str, Any]:
        current = url
        for redirect_count in range(self._policy.max_redirects + 1):
            target = self._target(current, path)
            resolved_ip = self._resolve_public(target, path)
            try:
                response = self._transport.fetch(target, resolved_ip, self._policy)
            except TimeoutError as exc:
                raise HostedImageFetchError(path, "image fetch timed out") from exc
            except OSError as exc:
                raise HostedImageFetchError(path, "image fetch failed") from exc
            if 300 <= response.status < 400:
                location = self._header(response.headers, "location")
                if not location:
                    raise HostedImageFetchError(path, "image redirect is missing a location")
                if redirect_count >= self._policy.max_redirects:
                    raise HostedImageFetchError(path, "image redirect limit exceeded")
                current = urllib.parse.urljoin(current, location)
                continue
            if response.status < 200 or response.status >= 300:
                raise HostedImageFetchError(path, f"image returned HTTP {response.status}")
            return self._verified_response(current, response, path)
        raise HostedImageFetchError(path, "image redirect limit exceeded")

    @staticmethod
    def _header(headers: Mapping[str, str], name: str) -> str | None:
        for key, value in headers.items():
            if key.lower() == name:
                return str(value)
        return None

    def _target(self, url: str, path: str) -> _Target:
        if not isinstance(url, str) or not url or any(ord(char) < 33 for char in url):
            raise HostedImageFetchError(path, "image URL is invalid")
        try:
            parsed = urllib.parse.urlsplit(url)
            hostname = parsed.hostname
        except ValueError as exc:
            raise HostedImageFetchError(path, "image URL is invalid") from exc
        if parsed.scheme != "https" or not hostname or parsed.username is not None or parsed.password is not None:
            raise HostedImageFetchError(path, "image URL must use credential-free HTTPS")
        try:
            port = parsed.port or 443
        except ValueError as exc:
            raise HostedImageFetchError(path, "image URL has an invalid port") from exc
        if port != 443:
            raise HostedImageFetchError(path, "image URL must use HTTPS port 443")
        request_target = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
        if not request_target.startswith("/"):
            raise HostedImageFetchError(path, "image URL is invalid")
        return _Target(url=urllib.parse.urlunsplit(("https", parsed.netloc, parsed.path or "/", parsed.query, "")), host=hostname, port=port, request_target=request_target)

    def _resolve_public(self, target: _Target, path: str) -> str:
        try:
            addresses = tuple(self._resolver(target.host, target.port))
        except (OSError, TimeoutError) as exc:
            raise HostedImageFetchError(path, "image host could not be resolved") from exc
        if not addresses:
            raise HostedImageFetchError(path, "image host could not be resolved")
        # A mixed DNS answer is unsafe: a retry, Happy Eyeballs implementation,
        # or future transport change must not select the private alternative.
        if any(not _is_public_destination(address) for address in addresses):
            raise HostedImageFetchError(path, "image destination is not publicly routable")
        return addresses[0]

    def _verified_response(self, final_url: str, response: TransportResponse, path: str) -> dict[str, Any]:
        content_type = (self._header(response.headers, "content-type") or "").split(";", 1)[0].strip().lower()
        if content_type == "image/jpg":
            content_type = "image/jpeg"
        if content_type not in REMOTE_IMAGE_CONTENT_TYPES:
            raise HostedImageFetchError(path, "image content type is not allowed")
        declared_length = self._header(response.headers, "content-length")
        if declared_length is not None:
            try:
                length = int(declared_length)
            except ValueError as exc:
                raise HostedImageFetchError(path, "image Content-Length is invalid") from exc
            if length < 0 or length > self._policy.max_bytes:
                raise HostedImageFetchError(path, "image exceeds the byte limit")
        if len(response.body) > self._policy.max_bytes:
            raise HostedImageFetchError(path, "image exceeds the byte limit")
        detected_type = image_signature_type(response.body)
        if detected_type is None:
            raise HostedImageFetchError(path, "image signature is not recognized")
        if detected_type != content_type:
            raise HostedImageFetchError(path, "image content type does not match its bytes")
        return {"url": final_url, "contentType": content_type, "bytes": len(response.body)}


def image_signature_type(body: bytes) -> str | None:
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


def _current_image_entries(seed: Mapping[str, Any]) -> Iterable[tuple[str, str]]:
    round_value = seed.get("round")
    if not isinstance(round_value, Mapping):
        return
    options = round_value.get("options")
    if not isinstance(options, list):
        return
    for option_index, option in enumerate(options):
        if not isinstance(option, Mapping):
            continue
        if isinstance(option.get("images"), list):
            for image_index, image in enumerate(option["images"]):
                if isinstance(image, Mapping) and isinstance(image.get("url"), str):
                    yield f"seed.round.options[{option_index}].images[{image_index}]", image["url"]
        elif isinstance(option.get("image"), Mapping) and isinstance(option["image"].get("url"), str):
            yield f"seed.round.options[{option_index}].image", option["image"]["url"]


def verify_remote_current_images(seed: Mapping[str, Any], fetcher: HostedImageFetcher) -> dict[str, Any]:
    """Fetch each unique active-round image once under the hosted policy."""

    manifest = list(_current_image_entries(seed))
    references: dict[str, list[str]] = {}
    for path, url in manifest:
        references.setdefault(url, []).append(path)
    urls = list(references)
    if not urls:
        return {"scope": "currentRound", "images": 0, "uniqueImages": 0, "verified": []}
    results: list[dict[str, Any] | None] = [None] * len(urls)
    errors: list[tuple[int, str]] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(fetcher.policy.max_concurrency, len(urls))) as executor:
        futures = {executor.submit(fetcher.fetch, url, path=references[url][0]): index for index, url in enumerate(urls)}
        for future in concurrent.futures.as_completed(futures):
            index = futures[future]
            try:
                results[index] = future.result()
            except HostedImageFetchError as exc:
                errors.append((index, f"{', '.join(references[urls[index]])}: {exc.reason}"))
    if errors:
        errors.sort(key=lambda item: item[0])
        raise HostedImageFetchError("image verification failed", "; ".join(message for _index, message in errors))
    return {"scope": "currentRound", "images": len(manifest), "uniqueImages": len(urls), "verified": [result for result in results if result is not None]}
