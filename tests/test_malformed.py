"""TDD: malformed-line-spike. A flood of *corrupt JSON* (lines that tried to be JSON and
failed) means a service is emitting garbage or the stream is corrupted — itself a signal.

Crucially, postgres/redis emit plain-text lines by design; those are NOT corrupt JSON and
must never feed the spike, or the detector would scream on every healthy startup. Only
lines that start with `{`/`[` but fail to parse count.
"""
from datetime import timedelta

from sre_agent.detect.malformed import MalformedSpikeDetector
from sre_agent.ingest.parser import LineParser
from sre_agent.ingest.window import SlidingWindow
from tests.helpers import T0, fast_config

PLAINTEXT = "2026-06-12 04:03:00.706 UTC [1] LOG:  database system is ready"
CORRUPT = '{"ts":"2026-06-12T04:03:00Z","service":"api","level":'  # truncated JSON
GOOD = '{"ts":"2026-06-12T04:03:00Z","service":"api","level":"INFO"}'


def cfg():
    c = fast_config()
    c.malformed_window_s = 60.0
    c.malformed_threshold = 5
    return c


def at(s):
    return T0 + timedelta(seconds=s)


# --- parser: distinguishes corrupt JSON from expected plain text -----------------
def test_corrupt_json_is_tracked_with_timestamp():
    parser = LineParser()
    assert parser.parse(CORRUPT, now=at(0)) is None
    assert parser.corrupt_since(at(-1)) == 1


def test_plaintext_counts_malformed_but_not_corrupt():
    parser = LineParser()
    assert parser.parse(PLAINTEXT, now=at(0)) is None
    assert parser.malformed_count == 1       # still counted as non-JSON
    assert parser.corrupt_since(at(-1)) == 0  # but NOT a corrupt-JSON spike signal


def test_good_and_blank_lines_are_not_corrupt():
    parser = LineParser()
    assert parser.parse(GOOD, now=at(0)) is not None
    assert parser.parse("", now=at(0)) is None
    assert parser.corrupt_since(at(-1)) == 0


def test_corrupt_since_respects_window():
    parser = LineParser()
    parser.parse(CORRUPT, now=at(0))
    parser.parse(CORRUPT, now=at(100))
    assert parser.corrupt_since(at(50)) == 1  # only the recent one


# --- detector --------------------------------------------------------------------
def test_detector_fires_on_corrupt_flood():
    parser = LineParser()
    for i in range(cfg().malformed_threshold):
        parser.parse(CORRUPT, now=at(10 + i))
    anomalies = MalformedSpikeDetector(cfg(), parser).check(SlidingWindow(), at(20))
    assert len(anomalies) == 1
    assert anomalies[0].signal_type == "malformed_spike"


def test_detector_quiet_below_threshold():
    parser = LineParser()
    for i in range(cfg().malformed_threshold - 1):
        parser.parse(CORRUPT, now=at(10 + i))
    assert MalformedSpikeDetector(cfg(), parser).check(SlidingWindow(), at(20)) == []


def test_detector_immune_to_plaintext_flood():
    """A healthy postgres/redis dumping plain text must not trip the spike."""
    parser = LineParser()
    for i in range(cfg().malformed_threshold * 5):
        parser.parse(PLAINTEXT, now=at(10 + i))
    assert MalformedSpikeDetector(cfg(), parser).check(SlidingWindow(), at(60)) == []


def test_detector_ignores_corrupt_outside_window():
    parser = LineParser()
    for i in range(cfg().malformed_threshold):
        parser.parse(CORRUPT, now=at(i))  # all old
    assert MalformedSpikeDetector(cfg(), parser).check(SlidingWindow(), at(500)) == []
