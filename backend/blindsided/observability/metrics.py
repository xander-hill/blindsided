from prometheus_client import Counter, Histogram


RPC_REQUESTS = Counter(
    "blindsided_rpc_requests_total",
    "Total number of gRPC requests handled by Blindsided",
    ["service", "method", "result"],
)

RPC_DURATION_SECONDS = Histogram(
    "blindsided_rpc_duration_seconds",
    "Time spent handling Blindsided gRPC requests",
    ["service", "method", "result"],
)

MUTATIONS = Counter(
    "blindsided_mutations_total",
    "Final outcomes of logical external auction mutations",
    ["operation", "outcome"],
)

CONCURRENCY_RETRIES = Counter(
    "blindsided_concurrency_retries_total",
    "Optimistic-concurrency retry outcomes",
    ["operation", "outcome"],
)

IDEMPOTENCY_REQUESTS = Counter(
    "blindsided_idempotency_requests_total",
    "Authoritative idempotency decisions for mutation requests",
    ["operation", "outcome"],
)
