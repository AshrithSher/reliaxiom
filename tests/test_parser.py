from sre_agent.ingest.parser import LineParser

GOOD = ('{"ts":"2026-06-11T14:32:05.123Z","service":"api","level":"ERROR",'
        '"event":"request_failed","message":"db down","request_id":"a1b2",'
        '"path":"/products","status":503,"error":"db_unreachable"}')


def test_parses_lab_line():
    parser = LineParser()
    record = parser.parse(GOOD)
    assert record is not None
    assert record.service == "api"
    assert record.status == 503
    assert record.is_error
    assert record.request_id == "a1b2"
    assert record.ts.year == 2026 and record.ts.tzinfo is not None
    assert record.raw["error"] == "db_unreachable"


def test_malformed_lines_counted_not_fatal():
    parser = LineParser()
    assert parser.parse("LOG: database system is ready to accept connections") is None
    assert parser.parse('{"truncated": ') is None
    assert parser.parse('[1,2,3]') is None
    assert parser.parse("") is None  # blank lines don't count as malformed
    assert parser.malformed_count == 3
    assert parser.parse(GOOD) is not None
    assert parser.parsed_count == 1


def test_status_500_is_error_even_at_info_level():
    parser = LineParser()
    record = parser.parse('{"ts":"2026-06-11T14:32:05Z","service":"gateway","level":"INFO","status":502}')
    assert record is not None and record.is_error


def test_missing_ts_gets_arrival_time():
    parser = LineParser()
    record = parser.parse('{"service":"api","level":"INFO"}')
    assert record is not None and record.ts.tzinfo is not None
