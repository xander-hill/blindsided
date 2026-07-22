from prometheus_client import Counter, Histogram


RPC_REQUESTS = Counter(
    "blindsided_rpc_requests_total",
    "Total number of gRPC requests handled by Blindsided",
    ["service", "method", "result"],
)

RPC_LATENCY_SECONDS = Histogram(
    "blindsided_rpc_latency_seconds",
    "Time spent handling Blindsided gRPC requests",
    ["service", "method"],
)

MUTATION_OUTCOMES = Counter(
    "blindsided_mutation_outcomes_total",
    "Outcomes of mutation requests handled by Blindsided",
    ["service", "method", "outcome"],
)

CONCURRENCY_RETRIES = Counter(
    "blindsided_concurrency_retries_total",
    "Mutation retries caused by optimistic concurrency conflicts",
    ["service", "method"],
)

REPLICATION_OPERATIONS = Counter(
    "blindsided_replication_operations_total",
    "Synchronous replication protocol phase outcomes",
    ["service", "phase", "outcome"],
)

CONTROLLER_FAILOVERS = Counter(
    "blindsided_controller_failovers_total",
    "Controller primary failover outcomes",
    ["service", "outcome"],
)
