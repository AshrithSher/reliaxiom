"""TopologyProvider SPI (ROADMAP P0.1, DECISIONS D-031): the dependency graph is read
through an interface, so it can come from a real source — a service mesh, a trace-graph, a
CMDB, or a Backstage catalog — without forking the engine. Correlation, the diagnosis
context, and the dashboard all consume a `TopologyMap`; only *where the map comes from*
changes behind this seam.

The default `StaticTopologyProvider` wraps the built-in `LAB_TOPOLOGY` literal. The
production-shaped `HttpTopologyProvider` follows the exact discipline of the telemetry
adapters (D-032) and the pollers (D-015): it **injects its HTTP transport** (so parsing is
unit-tested offline against canned JSON) and **degrades to a static fallback on any
failure** — a momentarily-unreachable topology source must never crash correlation or
fabricate a graph. A missing graph is honest; a wrong one is a lie the correlator would
trust to root the wrong service."""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Callable, Protocol, runtime_checkable

from sre_agent.incident.topology import LAB_TOPOLOGY, TopologyMap

# transport(url) -> (status_code, response_text). Injected for testability (mirrors the
# telemetry adapters' seam so a hosted, credentialed source swaps in the same way).
Transport = Callable[[str], tuple[int, str]]


@runtime_checkable
class TopologyProvider(Protocol):
    """Yields the current dependency graph. The engine reads the graph through this only."""

    def topology(self) -> TopologyMap: ...


class StaticTopologyProvider:
    """The default: a fixed in-process graph (the lab literal, or any provided map)."""

    def __init__(self, topology: TopologyMap = LAB_TOPOLOGY) -> None:
        self._topology = topology

    def topology(self) -> TopologyMap:
        return self._topology


class HttpTopologyProvider:
    """Fetches a `{service: [deps, ...]}` adjacency document over HTTP (service-mesh / CMDB /
    Backstage). A successful fetch is cached (topology is queried per candidate, so we do not
    re-fetch on every correlation); a failure is **not** cached — it returns the fallback and
    retries next time, so the real graph is picked up once the source recovers."""

    def __init__(self, base_url: str = "http://localhost:8080",
                 transport: Transport | None = None, timeout_s: float = 10.0,
                 path: str = "/topology", fallback: TopologyMap = LAB_TOPOLOGY) -> None:
        self._url = base_url.rstrip("/") + path
        self._transport = transport or _http_get(timeout_s)
        self._fallback = fallback
        self._cached: TopologyMap | None = None

    def topology(self) -> TopologyMap:
        if self._cached is not None:
            return self._cached
        fetched = self._fetch()
        if fetched is not None:
            self._cached = fetched
            return fetched
        return self._fallback

    def _fetch(self) -> TopologyMap | None:
        try:
            status, text = self._transport(self._url)
            if status >= 400 or not text.strip():
                return None
            doc = json.loads(text)
        except Exception:  # noqa: BLE001 — degrade, never raise (D-015)
            return None
        adjacency = _parse_adjacency(doc)
        return TopologyMap(adjacency) if adjacency is not None else None


def _parse_adjacency(doc: object) -> dict[str, list[str]] | None:
    """Validate the document is a dict of service -> list-of-string-deps. Anything else is
    untrusted and rejected (the caller degrades to the fallback) rather than half-parsed."""
    if not isinstance(doc, dict):
        return None
    adjacency: dict[str, list[str]] = {}
    for svc, deps in doc.items():
        if not isinstance(svc, str) or not isinstance(deps, list):
            return None
        if not all(isinstance(dep, str) for dep in deps):
            return None
        adjacency[svc] = list(deps)
    return adjacency


def _http_get(timeout_s: float) -> Transport:
    def get(url: str) -> tuple[int, str]:
        req = urllib.request.Request(url, method="GET")
        try:
            with urllib.request.urlopen(req, timeout=timeout_s) as resp:
                return resp.status, resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read().decode("utf-8", errors="replace")

    return get


def build_topology_provider(cfg) -> TopologyProvider:
    """Select the topology source from config. `static` (default) wraps the built-in
    LAB_TOPOLOGY; `http` fetches an adjacency document and degrades to LAB_TOPOLOGY."""
    if getattr(cfg, "topology_source", "static") == "http":
        return HttpTopologyProvider(cfg.topology_url, fallback=LAB_TOPOLOGY)
    return StaticTopologyProvider(LAB_TOPOLOGY)
