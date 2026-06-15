from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from sre_agent.models import Anomaly
from sre_agent.telemetry.sources import LogSource


class Detector(ABC):
    """One deterministic check, evaluated every tick. Stateless: all state lives in
    the log source (input) and the engine's debounce tracker (output side).

    Detectors read through the `LogSource` SPI, not a concrete window — `SlidingWindow`
    is the default in-memory impl, but a real backend (Loki) swaps in behind it (D-031).
    Metric/trace detectors additionally take a `MetricSource`/`TraceSource` at construction."""

    signal_type: str

    @abstractmethod
    def check(self, logs: LogSource, now: datetime) -> list[Anomaly]: ...
