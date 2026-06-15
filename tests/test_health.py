"""TDD: the self-health HTTP server (Maturity 9) — /healthz and /readyz reflect injected
liveness/readiness predicates; an unknown path is 404; a handler exception is a clean 500."""
from __future__ import annotations

import urllib.request

from sre_agent.health import HealthServer


def _get(port: int, path: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=2) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_healthz_and_readyz_reflect_predicates():
    state = {"alive": True, "ready": False}
    port = _free_port()
    srv = HealthServer(port, liveness=lambda: state["alive"], readiness=lambda: state["ready"])
    srv.start()
    try:
        assert _get(port, "/healthz")[0] == 200          # alive
        assert _get(port, "/readyz")[0] == 503           # not ready yet
        state["ready"] = True
        assert _get(port, "/readyz")[0] == 200           # now ready
        state["alive"] = False
        assert _get(port, "/healthz")[0] == 503          # dead-man's switch trips
        assert _get(port, "/nope")[0] == 404
    finally:
        srv.stop()


def test_handler_exception_is_500_not_a_crash():
    def boom():
        raise RuntimeError("kaboom")
    port = _free_port()
    srv = HealthServer(port, liveness=boom)
    srv.start()
    try:
        assert _get(port, "/healthz")[0] == 500          # contained, server still up
        assert _get(port, "/readyz")[0] == 200
    finally:
        srv.stop()
