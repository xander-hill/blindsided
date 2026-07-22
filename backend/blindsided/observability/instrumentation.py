from collections.abc import Callable
from contextvars import ContextVar
from functools import wraps
import logging
import time

import grpc

from blindsided.observability.metrics import (
    CONCURRENCY_RETRIES,
    IDEMPOTENCY_REQUESTS,
    MUTATIONS,
    RPC_DURATION_SECONDS,
    RPC_REQUESTS,
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
