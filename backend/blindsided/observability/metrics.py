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
