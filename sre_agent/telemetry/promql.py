"""Single source of truth for the PromQL the agent uses. Both the metric *detectors* and the
metric *recovery* verifier build their expressions here, so detection and verification can
never drift apart (you detect and you confirm-recovered on the exact same query)."""
from __future__ import annotations


def error_ratio_expr(service: str) -> str:
    """Fraction of 5xx responses for a service over the last minute (RED-errors)."""
    return (f'sum(rate(http_requests_total{{service="{service}",status=~"5.."}}[1m])) '
            f'/ sum(rate(http_requests_total{{service="{service}"}}[1m]))')


def latency_p95_expr(service: str) -> str:
    """p95 request latency in milliseconds from the duration histogram (RED-duration)."""
    return (f'histogram_quantile(0.95, sum(rate('
            f'http_request_duration_seconds_bucket{{service="{service}"}}[1m])) by (le)) * 1000')


def queue_saturation_expr() -> str:
    """Worker job-queue backlog gauge (USE-saturation)."""
    return "max(queue_depth)"
