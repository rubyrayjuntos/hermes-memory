#!/usr/bin/env python3
"""Serve the pane and proxy its API into the isolated Hermes container."""

from __future__ import annotations

import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen


class PaneProxyHandler(BaseHTTPRequestHandler):
    """Expose one static pane file and proxy API traffic to the loopback API."""

    html_path: Path

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/api/librarian/explorer")
            self.end_headers()
        elif path == "/fountain.html":
            self._serve_pane()
        elif path.startswith("/api/"):
            self._proxy_api()
        else:
            self.send_error(404)

    def do_HEAD(self) -> None:
        path = urlsplit(self.path).path
        if path == "/":
            self.send_response(302)
            self.send_header("Location", "/api/librarian/explorer")
            self.end_headers()
        elif path == "/fountain.html":
            self._serve_pane(send_body=False)
        else:
            self.send_error(404)

    def do_POST(self) -> None:
        self._proxy_api()

    def do_OPTIONS(self) -> None:
        self._proxy_api()

    def _serve_pane(self, send_body: bool = True) -> None:
        content = self.html_path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        if send_body:
            self.wfile.write(content)

    def _proxy_api(self) -> None:
        parsed = urlsplit(self.path)
        if not parsed.path.startswith("/api/"):
            self.send_error(404)
            return

        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            self.send_error(400, "Invalid Content-Length")
            return
        if content_length < 0 or content_length > 2_097_152:
            self.send_error(413, "Request body exceeds 2 MiB")
            return

        body = self.rfile.read(content_length) if content_length else None
        headers = {
            key: value
            for key, value in self.headers.items()
            if key.lower() not in {
                "connection",
                "content-length",
                "host",
                "transfer-encoding",
            }
        }
        headers["Accept-Encoding"] = "identity"
        request = Request(
            f"http://127.0.0.1:7890{parsed.path}"
            + (f"?{parsed.query}" if parsed.query else ""),
            data=body,
            headers=headers,
            method=self.command,
        )

        try:
            response = urlopen(request, timeout=30)
        except HTTPError as error:
            response = error
        except (URLError, TimeoutError) as error:
            self.send_error(502, f"Inspector API is unavailable: {error}")
            return

        with response:
            content = response.read()
            self.send_response(response.status)
            for key, value in response.headers.items():
                if key.lower() not in {
                    "connection",
                    "keep-alive",
                    "proxy-authenticate",
                    "proxy-authorization",
                    "te",
                    "trailers",
                    "transfer-encoding",
                    "upgrade",
                }:
                    self.send_header(key, value)
            self.end_headers()
            if content:
                self.wfile.write(content)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--html", required=True, type=Path)
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=17890, type=int)
    args = parser.parse_args()
    if not args.html.is_file():
        parser.error(f"pane file does not exist: {args.html}")

    PaneProxyHandler.html_path = args.html
    server = ThreadingHTTPServer((args.host, args.port), PaneProxyHandler)
    try:
        server.serve_forever()
    finally:
        server.server_close()
