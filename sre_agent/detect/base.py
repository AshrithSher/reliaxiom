from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime

from sre_agent.ingest.window import SlidingWindow
from sre_agent.models import Anomaly


class Detector(ABC):
    """One deterministic check, evaluated every tick. Stateless: all state lives in
    the window (input) and the engine's debounce tracker (output side)."""

    signal_type: str

    @abstractmethod
    def check(self, window: SlidingWindow, now: datetime) -> list[Anomaly]: ...
