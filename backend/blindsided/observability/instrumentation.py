from collections.abc import Callable
from contextvars import ContextVar
from contextlib import contextmanager
from functools import wraps
import logging
import time

import grpc

from blindsided.observability.metrics import (
    CONCURRENCY_RETRIES,
    IDEMPOTENCY_REQUESTS,
    MUTATIONS,
    COMMITS,
    REPLICATION_ATTEMPTS,
    REPLICATION_DURATION_SECONDS,
    CLUSTER_READY,
    FAILOVER_DURATION_SECONDS,
    FAILOVERS,
    HEALTHY_REPLICAS,
    PRIMARY_EPOCH,
    PROMOTION_ATTEMPTS,
    PROMOTION_DURATION_SECONDS,
    REGISTERED_REPLICAS,
    REPLICA_HEALTH_TRANSITIONS,
    SYNCHRONIZATION_ATTEMPTS,
    SYNCHRONIZATION_DURATION_SECONDS,
    RPC_DURATION_SECONDS,
    RPC_REQUESTS,
    ACTIVE_WATCH_STREAMS,
    STORAGE_EPOCH,
    STORAGE_READY,
    STORAGE_ROLE,
    WATCH_STREAMS,
    WATCH_UPDATES,
)


LOGGER = logging.getLogger(__name__)
BOUNDED_RESULTS = frozenset({
    "success",
    "failure",
    "unknown",
    "rejected",
    "conflict",
    "unavailable",
    "timeout",
    "stale_epoch",
})
ResultClassifier = Callable[[object], str]
_mutation_outcome: ContextVar[str | None] = ContextVar(
    "blindsided_mutation_outcome",
    default=None,
)
_replication_operation: ContextVar[str | None] = ContextVar(
    "blindsided_replication_operation",
    default=None,
)


def _exception_result(error: BaseException) -> str:
    if isinstance(error, grpc.RpcError):
        try:
            status = error.code()
        except Exception:
            return "failure"
        if status == grpc.StatusCode.DEADLINE_EXCEEDED:
            return "timeout"
        if status == grpc.StatusCode.UNAVAILABLE:
            return "unavailable"
    return "failure"


def _record_rpc(service: str, method: str, result: str, duration: float) -> None:
    """Record both collectors without allowing telemetry to affect the RPC."""
    try:
        RPC_REQUESTS.labels(
            service=service,
            method=method,
            result=result,
        ).inc()
        RPC_DURATION_SECONDS.labels(
            service=service,
            method=method,
            result=result,
        ).observe(duration)
    except Exception:
        LOGGER.exception("Failed to record RPC metrics")


def observe_rpc(
    service: str,
    method: str,
    result_classifier: ResultClassifier | None = None,
):
    """Instrument one unary RPC while preserving its behavior and metadata."""
    def decorator(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            started_at = time.perf_counter()
            result = "failure"
            try:
                response = handler(*args, **kwargs)
                try:
                    result = (
                        result_classifier(response)
                        if result_classifier is not None
                        else "success"
                    )
                except Exception:
                    LOGGER.exception("Failed to classify RPC response")
                    result = "failure"
                if result not in BOUNDED_RESULTS:
                    LOGGER.error("Unsupported RPC result from classifier: %r", result)
                    result = "failure"
                return response
            except BaseException as error:
                result = _exception_result(error)
                raise
            finally:
                _record_rpc(
                    service,
                    method,
                    result,
                    time.perf_counter() - started_at,
                )

        return wrapped

    return decorator


def _safe_increment(metric, **labels: str) -> None:
    try:
        metric.labels(**labels).inc()
    except Exception:
        LOGGER.exception("Failed to record metric")


def set_mutation_outcome(outcome: str) -> None:
    """Set an exact outcome determined inside the current mutation handler."""
    _mutation_outcome.set(outcome)


def observe_mutation(operation: str, result_classifier: ResultClassifier):
    """Record exactly one final outcome for an external logical mutation."""
    def decorator(handler):
        @wraps(handler)
        def wrapped(*args, **kwargs):
            token = _mutation_outcome.set(None)
            outcome = "failure"
            try:
                response = handler(*args, **kwargs)
                try:
                    outcome = _mutation_outcome.get() or result_classifier(response)
                except Exception:
                    LOGGER.exception("Failed to classify mutation response")
                return response
            finally:
                _safe_increment(MUTATIONS, operation=operation, outcome=outcome)
                _mutation_outcome.reset(token)

        return wrapped

    return decorator


def record_concurrency_retry(operation: str, outcome: str) -> None:
    _safe_increment(
        CONCURRENCY_RETRIES,
        operation=operation,
        outcome=outcome,
    )


def record_idempotency_decision(operation: str, outcome: str) -> None:
    _safe_increment(
        IDEMPOTENCY_REQUESTS,
        operation=operation,
        outcome=outcome,
    )


@contextmanager
def replication_operation(operation: str):
    token = _replication_operation.set(operation)
    try:
        yield
    finally:
        _replication_operation.reset(token)


def current_replication_operation() -> str | None:
    return _replication_operation.get()


def record_replication_attempt(
    operation: str,
    outcome: str,
    duration: float,
) -> None:
    try:
        REPLICATION_ATTEMPTS.labels(
            operation=operation,
            outcome=outcome,
        ).inc()
        REPLICATION_DURATION_SECONDS.labels(
            operation=operation,
            outcome=outcome,
        ).observe(duration)
    except Exception:
        LOGGER.exception("Failed to record replication metric")


def record_commit_outcome(outcome: str) -> None:
    operation = current_replication_operation()
    if operation is not None:
        _safe_increment(COMMITS, operation=operation, outcome=outcome)


def set_controller_gauges(
    *, registered: int, healthy: int, ready: bool, epoch: int
) -> None:
    try:
        REGISTERED_REPLICAS.set(registered)
        HEALTHY_REPLICAS.set(healthy)
        CLUSTER_READY.set(1 if ready else 0)
        PRIMARY_EPOCH.set(epoch)
    except Exception:
        LOGGER.exception("Failed to refresh controller gauges")


def record_health_transition(transition: str) -> None:
    _safe_increment(REPLICA_HEALTH_TRANSITIONS, transition=transition)


def record_timed_outcome(counter, histogram, outcome: str, duration: float) -> None:
    try:
        counter.labels(outcome=outcome).inc()
        histogram.labels(outcome=outcome).observe(duration)
    except Exception:
        LOGGER.exception("Failed to record timed controller outcome")


def record_failover(outcome: str, duration: float) -> None:
    record_timed_outcome(FAILOVERS, FAILOVER_DURATION_SECONDS, outcome, duration)


def record_promotion(outcome: str, duration: float) -> None:
    record_timed_outcome(
        PROMOTION_ATTEMPTS, PROMOTION_DURATION_SECONDS, outcome, duration
    )


def record_synchronization(outcome: str, duration: float) -> None:
    record_timed_outcome(
        SYNCHRONIZATION_ATTEMPTS,
        SYNCHRONIZATION_DURATION_SECONDS,
        outcome,
        duration,
    )


def refresh_storage_state_metrics(*, role: str, ready: bool, epoch: int) -> None:
    """Publish one authoritative storage-state snapshot without affecting it."""
    metric_role = role if role in {"primary", "backup"} else "unassigned"
    try:
        for candidate in ("primary", "backup", "unassigned"):
            STORAGE_ROLE.labels(role=candidate).set(candidate == metric_role)
        STORAGE_READY.set(1 if ready else 0)
        STORAGE_EPOCH.set(epoch)
    except Exception:
        LOGGER.exception("Failed to refresh storage state metrics")


def record_watch_stream_started() -> None:
    try:
        ACTIVE_WATCH_STREAMS.inc()
    except Exception:
        LOGGER.exception("Failed to record active watch stream")


def record_watch_stream_finished(outcome: str) -> None:
    _safe_increment(WATCH_STREAMS, outcome=outcome)
    try:
        ACTIVE_WATCH_STREAMS.dec()
    except Exception:
        LOGGER.exception("Failed to decrement active watch streams")


def record_watch_update() -> None:
    try:
        WATCH_UPDATES.inc()
    except Exception:
        LOGGER.exception("Failed to record watch update")
