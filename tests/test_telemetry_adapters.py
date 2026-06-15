"""TDD: the telemetry adapters (Prometheus / Loki / Tempo). Request shaping and response
parsing are exercised with an injected fake GET transport — no live backend touched. The
D-015 guarantee (degrade to None/empty, never raise) is asserted for every method."""
import json
from datetime import datetime, timezone

from sre_agent.telemetry.adapters import (
    LokiLogSource,
    PrometheusMetricSource,
    TempoTraceSource,
)
from sre_agent.telemetry.sources import LogSource, MetricSource, TraceSource

NOW = datetime(2026, 6, 13, 12, 0, 0, tzinfo=timezone.utc)
SINCE = datetime(2026, 6, 13, 11, 55, 0, tzinfo=timezone.utc)


class FakeGet:
    """Returns a canned (status, body) for the first route whose substring matches the url.
    `raises=True` makes the transport blow up, to prove adapters degrade rather than raise."""

    def __init__(self, routes, raises=False):
        self.routes = routes              # list of (substr, status, body_obj_or_text)
        self.calls = []
        self.raises = raises

    def __call__(self, url):
        self.calls.append(url)
        if self.raises:
            raise ConnectionError("backend unreachable")
        for substr, status, body in self.routes:
            if substr in url:
                text = body if isinstance(body, str) else json.dumps(body)
                return status, text
        return 404, '{"status":"error"}'


# --- Prometheus -------------------------------------------------------------------
def _prom_vector(value):
    return {"status": "success",
            "data": {"resultType": "vector",
                     "result": [{"metric": {"service": "api"}, "value": [1718280000, value]}]}}


def test_prometheus_instant_parses_scalar():
    t = FakeGet([("/api/v1/query", 200, _prom_vector("0.42"))])
    src = PrometheusMetricSource("http://prom", transport=t)
    assert src.instant("up", NOW) == 0.42
    assert "query=up" in t.calls[0]  # expr is url-encoded into the query


def test_prometheus_instant_none_on_empty_result():
    t = FakeGet([("/api/v1/query", 200, {"status": "success", "data": {"result": []}})])
    assert PrometheusMetricSource("http://prom", transport=t).instant("up", NOW) is None


def test_prometheus_range_parses_series():
    body = {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {}, "values": [[1718280000, "1.0"], [1718280060, "2.5"]]}]}}
    src = PrometheusMetricSource("http://prom", transport=FakeGet([("query_range", 200, body)]))
    points = src.range("rate(x[1m])", SINCE, NOW, 60)
    assert [p.value for p in points] == [1.0, 2.5]
    assert points[0].ts.tzinfo is not None  # UTC-aware


def test_prometheus_degrades_to_none_on_transport_error():
    src = PrometheusMetricSource("http://prom", transport=FakeGet([], raises=True))
    assert src.instant("up", NOW) is None
    assert src.range("up", SINCE, NOW, 60) == []


def test_prometheus_degrades_on_http_error_and_malformed_body():
    assert PrometheusMetricSource(
        "http://p", transport=FakeGet([("query", 500, "boom")])).instant("up", NOW) is None
    assert PrometheusMetricSource(
        "http://p", transport=FakeGet([("query", 200, "not json")])).instant("up", NOW) is None


def test_prometheus_satisfies_protocol():
    assert isinstance(PrometheusMetricSource(transport=FakeGet([])), MetricSource)


# --- Loki -------------------------------------------------------------------------
def _loki_line(service, offset_s, level="INFO", **extra):
    ts = SINCE.timestamp() + offset_s
    payload = {"ts": datetime.fromtimestamp(ts, tz=timezone.utc)
               .isoformat().replace("+00:00", "Z"),
               "service": service, "level": level, **extra}
    return [str(int(ts * 1e9)), json.dumps(payload)]


def _loki_body(*lines):
    return {"status": "success",
            "data": {"resultType": "streams",
                     "result": [{"stream": {"service": "api"}, "values": list(lines)}]}}


def _loki_source(routes):
    return LokiLogSource("http://loki", transport=FakeGet(routes),
                         clock=lambda: NOW)


def test_loki_records_parse_via_lineparser():
    body = _loki_body(_loki_line("api", 10), _loki_line("api", 20, level="ERROR", status=500))
    recs = _loki_source([("query_range", 200, body)]).records("api", SINCE)
    assert [r.service for r in recs] == ["api", "api"]
    assert recs[1].is_error  # status 500 → error


def test_loki_error_records_filters():
    body = _loki_body(_loki_line("api", 10), _loki_line("api", 20, level="ERROR"))
    errs = _loki_source([("query_range", 200, body)]).error_records("api", SINCE)
    assert len(errs) == 1 and errs[0].is_error


def test_loki_services_lists_label_values():
    body = {"status": "success", "data": ["api", "worker", "gateway"]}
    assert _loki_source([("/label/service/values", 200, body)]).services() == [
        "api", "worker", "gateway"]


def test_loki_degrades_to_empty_on_failure():
    src = LokiLogSource("http://loki", transport=FakeGet([], raises=True), clock=lambda: NOW)
    assert src.records("api", SINCE) == []
    assert src.services() == []
    assert src.last_seen("api") is None


def test_loki_satisfies_logsource_protocol():
    assert isinstance(_loki_source([]), LogSource)


def test_sliding_window_satisfies_logsource_protocol():
    # the keystone: the default in-memory impl conforms to the same SPI as Loki, so the
    # detectors are genuinely backend-agnostic (D-031).
    from sre_agent.ingest.window import SlidingWindow

    assert isinstance(SlidingWindow(), LogSource)


# --- Tempo ------------------------------------------------------------------------
def _tempo_metric(value):
    return {"status": "success",
            "data": {"result": [{"metric": {}, "values": [[1718280000, value]]}]}}


def test_tempo_error_rate_divides_error_by_total():
    # total rate 10/s, error rate 2/s → 0.2. Only the error query's TraceQL carries the
    # word "error" (url-encoded but the alphanumerics survive), so route on that.
    class OrderedGet(FakeGet):
        def __call__(self, url):
            self.calls.append(url)
            if "error" in url:
                return 200, json.dumps(_tempo_metric("2.0"))
            return 200, json.dumps(_tempo_metric("10.0"))

    src = TempoTraceSource("http://tempo", transport=OrderedGet([]), clock=lambda: NOW)
    assert src.error_rate("api", SINCE) == 0.2


def test_tempo_error_rate_zero_when_no_traffic():
    class ZeroGet(FakeGet):
        def __call__(self, url):
            return 200, json.dumps(_tempo_metric("0"))

    assert TempoTraceSource("http://t", transport=ZeroGet([]), clock=lambda: NOW).error_rate(
        "api", SINCE) == 0.0


def test_tempo_error_rate_none_on_failure():
    src = TempoTraceSource("http://t", transport=FakeGet([], raises=True), clock=lambda: NOW)
    assert src.error_rate("api", SINCE) is None


def test_tempo_trace_parses_otlp_spans():
    body = {"batches": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "api"}}]},
        "scopeSpans": [{"spans": [{
            "traceId": "abc", "spanId": "s1", "name": "GET /products",
            "startTimeUnixNano": "1718280000000000000",
            "endTimeUnixNano": "1718280000050000000",
            "status": {"code": 2}}]}]}]}
    trace = TempoTraceSource("http://t", transport=FakeGet([("/api/traces/", 200, body)])).trace("abc")
    assert trace is not None
    assert trace.services == ["api"]
    span = trace.spans[0]
    assert span.service == "api" and span.is_error and span.duration_ms == 50.0


def test_tempo_trace_none_on_failure():
    assert TempoTraceSource(
        "http://t", transport=FakeGet([], raises=True)).trace("abc") is None


def test_tempo_satisfies_protocol():
    assert isinstance(TempoTraceSource(transport=FakeGet([])), TraceSource)
