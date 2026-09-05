"""HTTP handler for the live viz API."""
from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import parse_qs, urlparse

from .graph_view import (
    DEFAULT_LIMIT,
    GHOST_MAX_K,
    GHOST_MAX_LIMIT,
    _allowed_cors_origin,
    clamp_limit,
    find_pane_dir,
    match_route,
    parse_vertex_id_param,
    safe_label,
)

logger = logging.getLogger("hermes_memory.graph_api")

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
        logger.info("%s - " + format, self.address_string(), *args)

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        allowed = _allowed_cors_origin(origin)
        if allowed is not None:
            self.send_header("Access-Control-Allow-Origin", allowed)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "accept, content-type")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self.send_response(204)
        self._cors()
        self.end_headers()

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def do_PATCH(self) -> None:  # noqa: N802
        self._dispatch()

    def do_DELETE(self) -> None:  # noqa: N802
        self._dispatch()

    def _json(self, code: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _bytes(self, code: int, body: bytes, content_type: str) -> None:
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _dispatch(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        route, params = match_route(self.command, parsed.path)
        from .graph_server import RUNTIME
        rt = RUNTIME
        try:
            if route == "not_found":
                self._json(404, {"error": "not found"})
                return
            if route == "not_implemented":
                self._json(501, {"error": "not implemented", "detail": "read-only viz API"})
                return
            if route == "pane":
                self._serve_pane("fountain.html")
                return
            if route == "pane_index":
                self._serve_pane("index.html")
                return
            if rt is None:
                self._json(503, {"error": "runtime not ready"})
                return
            if route == "health":
                self._json(200, rt.health())
                return
            if route == "librarian_health":
                self._json(200, rt.librarian_health())
                return
            if route == "stats":
                self._json(200, rt.stats())
                return
            if route == "graph_3d":
                label = safe_label((qs.get("label") or [""])[0])
                limit = clamp_limit((qs.get("limit") or [DEFAULT_LIMIT])[0])
                self._json(200, rt.graph_3d(label, limit))
                return
            if route == "ghost":
                k = clamp_limit((qs.get("k") or [5])[0], default=5)
                k = max(1, min(k, GHOST_MAX_K))
                # threshold is 0..0.99 float, not a limit clamp
                try:
                    thr = float((qs.get("threshold") or ["0.70"])[0])
                except Exception:
                    thr = 0.70
                lim = clamp_limit((qs.get("limit") or [200])[0], default=200)
                lim = max(10, min(lim, GHOST_MAX_LIMIT))
                self._json(200, rt.ghost(k, thr, lim))
                return
            if route == "chunks":
                fp = (qs.get("file_path") or [""])[0]
                limit = clamp_limit((qs.get("limit") or [4])[0], default=4)
                self._json(200, rt.chunks(fp, limit))
                return
            if route == "search":
                q = (qs.get("q") or [""])[0]
                k = clamp_limit((qs.get("k") or [8])[0], default=8)
                hops = clamp_limit((qs.get("hops") or [2])[0], default=2)
                self._json(200, rt.search(q, k, hops))
                return
            if route == "verify":
                self._json(200, rt.verify_readonly())
                return
            if route == "node":
                vid = parse_vertex_id_param(params["vid"])
                self._json(200, rt.node(vid))
                return
            if route == "audit":
                vid = parse_vertex_id_param(params["vid"])
                self._json(200, rt.audit(vid))
                return
            self._json(404, {"error": "not found"})
        except ValueError as exc:
            self._json(400, {"error": str(exc)})
        except Exception:
            logger.exception("request failed %s %s", self.command, parsed.path)
            self._json(500, {"error": "internal error"})

    def _serve_pane(self, name: str) -> None:
        pane = find_pane_dir()
        if pane is None:
            self._json(404, {"error": "pane html not found in repo docs/graph"})
            return
        path = pane / name
        if not path.is_file():
            path = pane / "fountain.html"
        if not path.is_file():
            path = pane / "3d.html"
        body = path.read_bytes()
        self._bytes(200, body, "text/html; charset=utf-8")
