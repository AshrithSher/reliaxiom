"""Real-time tailer on the combined Docker log stream.

Runs `docker compose logs -f` in the lab directory (or reads stdin for replay/tests)
on a daemon thread, parsing each line into the sliding window.
"""
from __future__ import annotations

import subprocess
import threading
from typing import IO

from sre_agent.ingest.parser import LineParser
from sre_agent.ingest.window import SlidingWindow


class Tailer:
    def __init__(self, window: SlidingWindow, parser: LineParser) -> None:
        self.window = window
        self.parser = parser
        self._thread: threading.Thread | None = None
        self._proc: subprocess.Popen[str] | None = None

    def start_docker(self, compose_dir: str) -> None:
        """Tail the live stream from now on (--since 5s avoids replaying history)."""
        self._proc = subprocess.Popen(
            ["docker", "compose", "logs", "-f", "--no-color", "--no-log-prefix", "--since", "5s"],
            cwd=compose_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        assert self._proc.stdout is not None
        self._start_thread(self._proc.stdout)

    def start_stream(self, stream: IO[str]) -> None:
        self._start_thread(stream)

    def _start_thread(self, stream: IO[str]) -> None:
        def run() -> None:
            for line in stream:
                record = self.parser.parse(line)
                if record is not None:
                    self.window.append(record)

        self._thread = threading.Thread(target=run, daemon=True, name="log-tailer")
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stop(self) -> None:
        if self._proc is not None:
            self._proc.terminate()
