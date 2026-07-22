from collections.abc import Callable
from functools import wraps
import logging
import time

import grpc

from blindsided.observability.metrics import RPC_DURATION_SECONDS, RPC_REQUESTS


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
