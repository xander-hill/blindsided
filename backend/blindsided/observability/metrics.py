from prometheus_client import Counter, Gauge, Histogram


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

REPLICATION_ATTEMPTS = Counter(
    "blindsided_replication_attempts_total",
    "Primary-to-designated-backup replication attempts",
    ["operation", "outcome"],
)

REPLICATION_DURATION_SECONDS = Histogram(
    "blindsided_replication_duration_seconds",
    "Duration of primary-to-designated-backup replication attempts",
    ["operation", "outcome"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0),
)

COMMITS = Counter(
    "blindsided_commits_total",
    "Final primary commit coordination outcomes",
    ["operation", "outcome"],
)

REGISTERED_REPLICAS = Gauge(
    "blindsided_registered_replicas",
    "Replicas in authoritative controller membership",
)

HEALTHY_REPLICAS = Gauge(
    "blindsided_healthy_replicas",
    "Registered replicas currently considered healthy by the controller",
)

CLUSTER_READY = Gauge(
    "blindsided_cluster_ready",
    "Whether the authoritative primary assignment is ready",
)

PRIMARY_EPOCH = Gauge(
    "blindsided_primary_epoch",
    "Current authoritative controller epoch",
)

REPLICA_HEALTH_TRANSITIONS = Counter(
    "blindsided_replica_health_transitions_total",
    "Authoritative replica membership and health transitions",
    ["transition"],
)

FAILOVERS = Counter(
    "blindsided_failovers_total",
    "Logical controller failover outcomes",
    ["outcome"],
)

FAILOVER_DURATION_SECONDS = Histogram(
    "blindsided_failover_duration_seconds",
    "Logical controller failover duration",
    ["outcome"],
)

PROMOTION_ATTEMPTS = Counter(
    "blindsided_promotion_attempts_total",
    "Candidate promotion attempt outcomes",
    ["outcome"],
)

PROMOTION_DURATION_SECONDS = Histogram(
    "blindsided_promotion_duration_seconds",
    "Candidate promotion attempt duration",
    ["outcome"],
)

SYNCHRONIZATION_ATTEMPTS = Counter(
    "blindsided_synchronization_attempts_total",
    "Replacement-backup synchronization attempt outcomes",
    ["outcome"],
)

SYNCHRONIZATION_DURATION_SECONDS = Histogram(
    "blindsided_synchronization_duration_seconds",
    "Replacement-backup synchronization attempt duration",
    ["outcome"],
)
