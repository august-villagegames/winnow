#!/usr/bin/env python3
"""Disposable Work Package 0 bounded-wait probe.

This deliberately tiny stdlib server is not a Winnow coordinator.  It exposes
enough JSON-RPC-shaped Streamable HTTP MCP surface to establish and release a
bounded wait, plus a browser endpoint that releases the next waiter.  It keeps
only process-local counters and must never be deployed as the remote service.

Run ``python3 remote/probes/mcp_browser_probe.py --self-test`` to exercise two
browser-driven wait cycles against localhost.  Run without ``--self-test`` to
serve it for a manually configured, disposable remote-host conformance probe.
"""

from __future__ import annotations

import argparse
import json
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class ProbeState:
    """Only the transient state required to release one bounded wait at a time."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.waiting = threading.Event()
        self.release = threading.Event()
        self.wait_count = 0
        self.release_count = 0
        self.resume_count = 0

    def wait(self, seconds: float) -> dict[str, Any]:
        with self.lock:
            self.wait_count += 1
            cycle = self.wait_count
            self.release.clear()
            self.waiting.set()
        released = self.release.wait(seconds)
        with self.lock:
            self.waiting.clear()
            if released:
                self.resume_count += 1
        return {"status": "continue_requested" if released else "still_waiting", "cycle": cycle}

    def continue_waiter(self) -> tuple[int, bool]:
        with self.lock:
            active = self.waiting.is_set()
            if active:
                self.release_count += 1
                self.release.set()
            return self.release_count, active


def _json_bytes(value: Any) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def make_handler(state: ProbeState) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, _format: str, *_args: object) -> None:
            # Do not retain request paths, headers, or addresses in the probe.
            return

        def _send_json(self, status: int, value: Any) -> None:
            body = _json_bytes(value)
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self) -> Any:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 8_192:
                raise ValueError("body too large")
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self) -> None:  # noqa: N802
            if self.path == "/probe-page":
                body = b"<main>Probe page URL is visible before bounded wait.</main>"
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self._send_json(404, {"error": "not_found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                payload = self._read_json()
            except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
                self._send_json(400, {"error": "invalid_json"})
                return
            if self.path == "/browser/continue":
                releases, active = state.continue_waiter()
                self._send_json(200 if active else 409, {"accepted": active, "releases": releases})
                return
            if self.path != "/mcp" or not isinstance(payload, dict):
                self._send_json(404, {"error": "not_found"})
                return
            request_id = payload.get("id")
            method = payload.get("method")
            if method == "initialize":
                result = {"protocolVersion": "2025-03-26", "capabilities": {"tools": {}}, "serverInfo": {"name": "winnow-wp0-probe", "version": "0"}}
            elif method == "tools/list":
                result = {"tools": [{"name": "wait_for_browser", "description": "Disposable bounded wait probe; returns when browser endpoint releases it.", "inputSchema": {"type": "object", "additionalProperties": False, "properties": {"maxWaitSeconds": {"type": "number", "minimum": 0.1, "maximum": 5}}, "required": ["maxWaitSeconds"]}}]}
            elif method == "tools/call" and isinstance(payload.get("params"), dict) and payload["params"].get("name") == "wait_for_browser":
                arguments = payload["params"].get("arguments", {})
                if not isinstance(arguments, dict) or not isinstance(arguments.get("maxWaitSeconds"), (int, float)):
                    self._send_json(400, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32602, "message": "maxWaitSeconds is required"}})
                    return
                event = state.wait(float(arguments["maxWaitSeconds"]))
                result = {"content": [{"type": "text", "text": json.dumps(event, separators=(",", ":"))}], "structuredContent": event}
            else:
                self._send_json(400, {"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "method not found"}})
                return
            self._send_json(200, {"jsonrpc": "2.0", "id": request_id, "result": result})

    return Handler


def post_json(url: str, payload: dict[str, Any], timeout: float = 10) -> dict[str, Any]:
    request = urllib.request.Request(url, data=_json_bytes(payload), headers={"Content-Type": "application/json", "Accept": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def self_test() -> None:
    state = ProbeState()
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}"
    try:
        with urllib.request.urlopen(base_url + "/probe-page", timeout=2) as response:
            assert response.status == 200
        initialize = post_json(base_url + "/mcp", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
        assert initialize["result"]["serverInfo"]["name"] == "winnow-wp0-probe"
        listed = post_json(base_url + "/mcp", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        assert listed["result"]["tools"][0]["name"] == "wait_for_browser"
        received: list[dict[str, Any]] = []

        def waiter(cycle: int) -> None:
            received.append(post_json(base_url + "/mcp", {"jsonrpc": "2.0", "id": cycle + 2, "method": "tools/call", "params": {"name": "wait_for_browser", "arguments": {"maxWaitSeconds": 2}}}, timeout=4)["result"]["structuredContent"])

        for cycle in (1, 2):
            waiting_thread = threading.Thread(target=waiter, args=(cycle,))
            waiting_thread.start()
            assert state.waiting.wait(1), "waiter did not become active"
            time.sleep(0.15)  # representative idle before a browser action
            released = post_json(base_url + "/browser/continue", {})
            assert released["accepted"] is True
            waiting_thread.join(3)
            assert not waiting_thread.is_alive(), "bounded wait did not resume"
        assert [event["status"] for event in received] == ["continue_requested", "continue_requested"]
        assert [event["cycle"] for event in received] == [1, 2]
        assert state.resume_count == 2
        print(json.dumps({"result": "pass", "cycles": 2, "idleSeconds": 0.15, "maxWaitSeconds": 2, "browserPageVisibleBeforeWait": True}, separators=(",", ":")))
    finally:
        server.shutdown()
        server.server_close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Disposable WP0 MCP/browser bounded-wait probe")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--port", type=int, default=8787)
    args = parser.parse_args()
    if args.self_test:
        self_test()
        return
    state = ProbeState()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), make_handler(state))
    print(f"http://127.0.0.1:{server.server_port}/mcp")
    try:
        server.serve_forever()
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
