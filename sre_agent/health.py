"""A tiny self-health HTTP server for k8s probes (Maturity 9) and, later, Prometheus scraping
and SLO signals (Maturity 11). Stdlib only — no framework, no extra dependency — running on a
daemon thread so it can never block or crash the detection loop.

Routes are pluggable: Maturity 9 registers `/healthz` (liveness) and `/readyz` (readiness);
Maturity 11 registers `/metrics` on the same server. Each handler is a zero-arg callable
returning `(status_code, content_type, body)`; an exception in a handler is caught and turned
into a 500 so a health probe can never take the agent down."""
from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable

Handler = Callable[[], "tuple[int, str, str]"]


def _ok_if(predicate: Callable[[], bool], ok_body: str = "ok",
           bad_body: str = "unavailable") -> Handler:
    def handler() -> tuple[int, str, str]:
        healthy = bool(predicate())
        return (200 if healthy else 503, "text/plain",
                f"{ok_body if healthy else bad_body}\n")
    return handler


class HealthServer:
    def __init__(self, port: int, *, liveness: Callable[[], bool] | None = None,
                 readiness: Callable[[], bool] | None = None) -> None:
        self._port = port
        self._routes: dict[str, Handler] = {}
        self.register("/healthz", _ok_if(liveness or (lambda: True), "alive", "dead"))
        self.register("/readyz", _ok_if(readiness or (lambda: True), "ready", "not ready"))
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def register(self, path: str, handler: Handler) -> None:
        self._routes[path] = handler

    def start(self) -> None:
        routes = self._routes

        class _H(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                handler = routes.get(self.path.split("?")[0])
                if handler is None:
                    self.send_response(404)
                    self.end_headers()
                    return
                try:
                    code, ctype, body = handler()
                except Exception:  # noqa: BLE001 — a probe must never wedge the agent
                    code, ctype, body = 500, "text/plain", "handler error\n"
                payload = body.encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *_args) -> None:  # silence default stderr access logging
                return

        self._httpd = ThreadingHTTPServer(("0.0.0.0", self._port), _H)
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True,
                                        name="sre-health")
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
