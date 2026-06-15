"""Container-down detector — the deterministic signal that a stateful dependency is simply
*gone*, regardless of how its consumers happen to log.

Why this exists (DECISIONS.md D-036): every other detector reads the log/metric/trace stream,
so "a dependency is down" only becomes a ticket if its consumers flood enough errors to trip a
rate threshold. Two real outages don't:
  - **redis down** — the worker is the only consumer and it retries on a slow fixed cadence
    (~one error every few seconds), which sits below `error_rate_threshold`;
  - **postgres down while the auth tier is also failing** — requests 503 at auth and never
    reach the db, so there is *no* db error to count.
In both cases the poller already *knows* the container is down (the heartbeat prints
`DOWN=postgres`). This detector turns that known fact into a candidate.

It emits the dependency's error code (`db_unreachable` / `redis_unreachable`) so the correlator
attributes it to the same `service:unreachable` fingerprint the log path uses — a container-down
candidate and any downstream error-rate candidate collapse into ONE incident (invariant #3).

It reads the polled `SignalStore`, not logs; the `check(logs, now)` signature is unchanged, so
it slots into the same engine as every other detector and inherits debounce, cooldown, and
action-suppression (it will not alarm on the brief down-state the agent's own restart causes —
invariant #5).
"""
from __future__ import annotations

from datetime import datetime

from sre_agent.config import Config
from sre_agent.detect.base import Detector
from sre_agent.models import Anomaly
from sre_agent.poll.store import SignalStore
from sre_agent.telemetry.sources import LogSource


class ContainerDownDetector(Detector):
    signal_type = "container_down"

    def __init__(self, cfg: Config, signals: SignalStore) -> None:
        self._signals = signals
        # container name -> dependency error code (so correlation roots it correctly)
        self._codes = dict(cfg.container_down_services)

    def check(self, logs: LogSource, now: datetime) -> list[Anomaly]:
        snap = self._signals.latest()
        if snap is None:
            return []   # no poll reading yet — never fabricate a down-state
        anomalies: list[Anomaly] = []
        for name, code in self._codes.items():
            cs = snap.containers.get(name)
            if cs is None:
                continue   # poller didn't report this container this cycle — stay silent
            if cs.status != "running":
                anomalies.append(Anomaly(
                    service=name,
                    signal_type=self.signal_type,
                    detail=(f"container {name} is {cs.status} (expected running) — "
                            f"{name} is unreachable"),
                    evidence=[f"{name} container status={cs.status} at {snap.ts.isoformat()}"],
                    error_codes=[code],
                ))
        return anomalies
