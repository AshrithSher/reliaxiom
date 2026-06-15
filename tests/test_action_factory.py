"""TDD: config -> action backend / rate limiter wiring. Selecting a substrate is config, not
code; the defaults keep the lab on docker + atomic SQLite (offline suite untouched)."""
import pytest

from sre_agent.action.backend import DockerActionBackend
from sre_agent.action.factory import build_action_backend, build_rate_limiter
from sre_agent.action.k8s_backend import KubernetesActionBackend
from sre_agent.action.ratelimit import SqliteRateLimiter
from sre_agent.changelog import ChangeLog
from sre_agent.config import Config


def test_default_backend_is_docker():
    assert isinstance(build_action_backend(Config()), DockerActionBackend)


def test_kubernetes_backend_selected_by_config(monkeypatch):
    monkeypatch.setenv("SRE_KUBE_TOKEN", "tok")
    cfg = Config()
    cfg.action_backend = "kubernetes"
    cfg.kube_api_server = "https://127.0.0.1:6443"
    assert isinstance(build_action_backend(cfg), KubernetesActionBackend)


def test_default_rate_limiter_is_sqlite_and_enforces_cap(tmp_path):
    from datetime import datetime, timezone
    cfg = Config()
    cfg.max_restarts_per_hour = 1
    rl = build_rate_limiter(cfg, ChangeLog(tmp_path / "changes.db"))
    assert isinstance(rl, SqliteRateLimiter)
    now = datetime(2026, 6, 14, tzinfo=timezone.utc)
    assert rl.try_consume("restart_container", "api", now).allowed
    assert not rl.try_consume("restart_container", "api", now).allowed   # cap=1


def test_postgres_rate_limiter_selected_by_config(tmp_path, monkeypatch):
    # Maturity 9 / D-041: guardrail_store=postgres now builds the fleet-atomic limiter (no
    # longer NotImplementedError). It needs a DSN but must not connect at construction-select
    # time in this unit test — we only assert the factory picks the Postgres class.
    monkeypatch.setenv("SRE_STATE_DSN", "host=localhost port=5433 dbname=sre user=sre password=sre")
    cfg = Config()
    cfg.guardrail_store = "postgres"
    captured = {}

    import sre_agent.action.factory as factory

    class _FakePgLimiter:
        def __init__(self, cap, dsn):
            captured["cap"], captured["dsn"] = cap, dsn

    monkeypatch.setattr("sre_agent.action.pg_ratelimit.PostgresRateLimiter", _FakePgLimiter)
    rl = factory.build_rate_limiter(cfg, ChangeLog(tmp_path / "changes.db"))
    assert isinstance(rl, _FakePgLimiter)
    assert captured["cap"] == cfg.max_restarts_per_hour
    assert "port=5433" in captured["dsn"]
