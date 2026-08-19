from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "remote" / "src"))

from winnow_remote.security import (  # noqa: E402
    HostedImageFetchError,
    HostedImageFetcher,
    HostedImagePolicy,
    TransportResponse,
    verify_remote_current_images,
)


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=")


class RecordingTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def fetch(self, target, resolved_ip, policy):
        self.calls.append((target.host, resolved_ip, target.request_target, policy.connect_timeout_seconds, policy.read_timeout_seconds))
        next_response = self.responses.pop(0)
        if isinstance(next_response, BaseException):
            raise next_response
        return next_response


def png_response(*, headers=None, body=PNG, status=200):
    return TransportResponse(status=status, headers={"Content-Type": "image/png", **(headers or {})}, body=body)


class HostedImageSecurityTests(unittest.TestCase):
    def make_fetcher(self, responses, resolver=lambda _host, _port: ("93.184.216.34",), **policy):
        transport = RecordingTransport(responses)
        return HostedImageFetcher(resolver=resolver, transport=transport, policy=HostedImagePolicy(**policy)), transport

    def test_private_metadata_and_mixed_dns_addresses_never_connect_or_echo_url(self):
        private_addresses = ("127.0.0.1", "10.0.0.1", "100.64.0.1", "169.254.169.254", "192.0.0.9", "::1", "::ffff:127.0.0.1", "fc00::1", "fe80::1", "ff02::1")
        for address in private_addresses:
            with self.subTest(address=address):
                fetcher, transport = self.make_fetcher([png_response()], resolver=lambda _host, _port, address=address: (address,))
                with self.assertRaisesRegex(HostedImageFetchError, "not publicly routable") as raised:
                    fetcher.fetch("https://metadata.example/credential.png", path="seed.round.options[0].image")
                self.assertNotIn("metadata.example", str(raised.exception))
                self.assertEqual(transport.calls, [])

        fetcher, transport = self.make_fetcher([png_response()], resolver=lambda _host, _port: ("93.184.216.34", "127.0.0.1"))
        with self.assertRaises(HostedImageFetchError):
            fetcher.fetch("https://mixed.example/image.png", path="seed.round.options[0].image")
        self.assertEqual(transport.calls, [])

    def test_redirect_revalidates_and_pins_each_destination(self):
        def resolver(host, _port):
            return {"origin.example": ("93.184.216.34",), "cdn.example": ("8.8.8.8",)}[host]

        fetcher, transport = self.make_fetcher(
            [
                TransportResponse(status=302, headers={"Location": "https://cdn.example/image.png"}, body=b""),
                png_response(),
            ],
            resolver=resolver,
        )
        result = fetcher.fetch("https://origin.example/start", path="seed.round.options[0].image")
        self.assertEqual(result["url"], "https://cdn.example/image.png")
        self.assertEqual([(host, ip) for host, ip, *_rest in transport.calls], [("origin.example", "93.184.216.34"), ("cdn.example", "8.8.8.8")])

    def test_redirect_to_private_and_too_many_redirects_are_rejected(self):
        fetcher, transport = self.make_fetcher(
            [TransportResponse(status=302, headers={"Location": "https://private.example/image.png"}, body=b"")],
            resolver=lambda host, _port: ("93.184.216.34",) if host == "origin.example" else ("169.254.169.254",),
        )
        with self.assertRaisesRegex(HostedImageFetchError, "not publicly routable"):
            fetcher.fetch("https://origin.example/start", path="seed.round.options[0].image")
        self.assertEqual(len(transport.calls), 1)

        redirects = [TransportResponse(status=302, headers={"Location": "https://origin.example/next"}, body=b"") for _ in range(4)]
        fetcher, _transport = self.make_fetcher(redirects)
        with self.assertRaisesRegex(HostedImageFetchError, "redirect limit"):
            fetcher.fetch("https://origin.example/start", path="seed.round.options[0].image")

    def test_content_length_body_mime_signature_and_timeout_boundaries(self):
        cases = [
            (png_response(headers={"Content-Length": str(9)}), {"max_bytes": 8}, "byte limit"),
            (png_response(body=PNG + b"x"), {"max_bytes": len(PNG)}, "byte limit"),
            (TransportResponse(status=200, headers={"Content-Type": "text/html"}, body=PNG), {}, "content type"),
            (TransportResponse(status=200, headers={"Content-Type": "image/png"}, body=b"<html>"), {}, "signature"),
            (TransportResponse(status=200, headers={"Content-Type": "image/gif"}, body=PNG), {}, "does not match"),
        ]
        for response, policy, message in cases:
            with self.subTest(message=message):
                fetcher, _transport = self.make_fetcher([response], **policy)
                with self.assertRaisesRegex(HostedImageFetchError, message):
                    fetcher.fetch("https://origin.example/image", path="seed.round.options[0].image")

        fetcher, transport = self.make_fetcher([TimeoutError("timed out")], connect_timeout_seconds=1.5, read_timeout_seconds=2.5)
        with self.assertRaisesRegex(HostedImageFetchError, "timed out"):
            fetcher.fetch("https://origin.example/image", path="seed.round.options[0].image")
        self.assertEqual(transport.calls[0][-2:], (1.5, 2.5))

    def test_credentialed_non_https_and_nonstandard_ports_are_rejected_before_dns(self):
        for url in ("http://origin.example/image", "https://user:password@origin.example/image", "https://origin.example:8443/image"):
            with self.subTest(url=url):
                fetcher, transport = self.make_fetcher([png_response()])
                with self.assertRaises(HostedImageFetchError):
                    fetcher.fetch(url, path="seed.round.options[0].image")
                self.assertEqual(transport.calls, [])

    def test_current_round_verification_deduplicates_and_returns_stable_order(self):
        seed = {
            "round": {
                "options": [
                    {"image": {"url": "https://origin.example/one.png"}},
                    {"images": [{"url": "https://origin.example/two.png"}, {"url": "https://origin.example/one.png"}]},
                ]
            }
        }
        fetcher, transport = self.make_fetcher([png_response(), png_response()], max_concurrency=1)
        result = verify_remote_current_images(seed, fetcher)
        self.assertEqual((result["images"], result["uniqueImages"]), (3, 2))
        self.assertEqual(len(transport.calls), 2)
        self.assertEqual([item["url"] for item in result["verified"]], ["https://origin.example/one.png", "https://origin.example/two.png"])


if __name__ == "__main__":
    unittest.main()
