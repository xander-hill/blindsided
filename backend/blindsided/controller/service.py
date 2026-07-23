from dataclasses import dataclass, field
from enum import Enum
import logging
import threading
import time

import grpc

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc
from blindsided.observability.instrumentation import (
    observe_rpc,
    record_failover,
    record_health_transition,
    record_promotion,
    record_synchronization,
    set_controller_gauges,
)


LOGGER = logging.getLogger(__name__)


def _log_event(level: int, event: str, **fields) -> None:
    def render(value):
        if isinstance(value, bool):
            return str(value).lower()
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, (list, tuple, set)):
            return ",".join(str(item) for item in value) or "none"
        return "none" if value is None else str(value)

    LOGGER.log(
        level,
        "event=%s %s",
        event,
        " ".join(f"{key}={render(value)}" for key, value in fields.items()),
    )


def _classify_success(response) -> str:
    return "success" if response.success else "failure"


def _rpc_status(error: grpc.RpcError):
    try:
        return error.code()
    except Exception:
        return None

HEARTBEAT_RPC_TIMEOUT_SECONDS = 2.0
PROMOTION_RPC_TIMEOUT_SECONDS = 5.0
SYNCHRONIZATION_RPC_TIMEOUT_SECONDS = 30.0


class ReplicaSyncStatus(Enum):
    UNSYNCHRONIZED = "unsynchronized"
    SYNCHRONIZED = "synchronized"


class PrimaryStatus(Enum):
    PROMOTING = "promoting"
    REPROTECTING = "reprotecting"
    READY = "ready"


@dataclass
class PrimaryAssignment:
    # node_id remains the stored field for compatibility with existing callers.
    node_id: str
    epoch: int
    status: PrimaryStatus
    sync_backup_address: str | None = None
    replacement_candidate_address: str | None = None
    recovery_generation: int = 0
    eligible_backup_addresses: tuple[str, ...] = field(default_factory=tuple)
    attempted_backup_addresses: set[str] = field(default_factory=set)

    @property
    def primary_address(self) -> str:
        return self.node_id


@dataclass
class ReplicaRecord:
    address: str
    last_seen: float
    sync_status: ReplicaSyncStatus
    synchronized_epoch: int = 0
    consecutive_heartbeat_failures: int = 0

    @property
    def promotion_eligible(self) -> bool:
        return self.sync_status == ReplicaSyncStatus.SYNCHRONIZED


class ControllerService(pb2_grpc.ClusterControllerServicer):
    """Tracks storage replicas and promotes a new primary after failures."""

    def __init__(
        self,
        *,
        heartbeat_timeout: float = HEARTBEAT_RPC_TIMEOUT_SECONDS,
        promotion_timeout: float = PROMOTION_RPC_TIMEOUT_SECONDS,
        synchronization_timeout: float = SYNCHRONIZATION_RPC_TIMEOUT_SECONDS,
    ):
        self.lock = threading.Lock()
        self.nodes: dict[str, ReplicaRecord] = {}
        self.primary_assignment: PrimaryAssignment | None = None
        self.last_primary_epoch = 0
        self._heartbeat_timeout = heartbeat_timeout
        self._promotion_timeout = promotion_timeout
        self._synchronization_timeout = synchronization_timeout
        self._election_in_progress = False
        self._failover_started_at: float | None = None
        self._promotion_started_at: dict[tuple[str, int], float] = {}

        set_controller_gauges(registered=0, healthy=0, ready=False, epoch=0)

    def _refresh_gauges_locked(self) -> None:
        assignment = self.primary_assignment
        set_controller_gauges(
            registered=len(self.nodes),
            healthy=sum(
                node.consecutive_heartbeat_failures == 0
                for node in self.nodes.values()
            ),
            ready=bool(assignment and assignment.status == PrimaryStatus.READY),
            epoch=self.last_primary_epoch,
        )

    def _finish_failover_locked(self, outcome: str) -> None:
        started_at = self._failover_started_at
        if started_at is not None:
            self._failover_started_at = None
            record_failover(outcome, time.perf_counter() - started_at)

    def _finish_promotion_locked(
        self, candidate_address: str, epoch: int, outcome: str
    ) -> None:
        started_at = self._promotion_started_at.pop(
            (candidate_address, epoch), None
        )
        if started_at is not None:
            record_promotion(outcome, time.perf_counter() - started_at)

    # Public controller RPCs

    @observe_rpc("controller", "RegisterNode", _classify_success)
    def RegisterNode(self, request, context):
        address = request.address.strip()
        if not address:
            return pb2.RegisterResponse(
                success=False,
                message="Replica address is required.",
            )
        reprotection_args: tuple[str, int] | None = None
        with self.lock:
            existing = self.nodes.get(address)
            reported_epoch = max(0, request.epoch)
            current_assignment = self.primary_assignment
            reported_synchronized = (
                request.role == "backup" and reported_epoch > 0
                and (
                    current_assignment is None
                    or address == current_assignment.sync_backup_address
                    or address
                    == current_assignment.replacement_candidate_address
                )
            )
            self.nodes[address] = ReplicaRecord(
                address=address,
                last_seen=time.time(),
                sync_status=(
                    ReplicaSyncStatus.SYNCHRONIZED
                    if reported_synchronized
                    else ReplicaSyncStatus.UNSYNCHRONIZED
                ),
                synchronized_epoch=reported_epoch if reported_synchronized else 0,
            )
            if existing is None:
                record_health_transition("registered")
            elif existing.consecutive_heartbeat_failures > 0:
                record_health_transition("unhealthy_to_healthy")
            if (
                self.primary_assignment is not None
                and reported_epoch > self.primary_assignment.epoch
            ):
                invalidated = self.primary_assignment
                LOGGER.warning(
                    "Invalidating recovered primary %s at epoch %s after %s "
                    "reported higher epoch %s",
                    self.primary_assignment.primary_address,
                    self.primary_assignment.epoch,
                    address,
                    reported_epoch,
                )
                self._finish_promotion_locked(
                    invalidated.primary_address,
                    invalidated.epoch,
                    "failed",
                )
                self.primary_assignment = None
                self._election_in_progress = False
                self._finish_failover_locked("failed")
            if reported_epoch > self.last_primary_epoch:
                self.last_primary_epoch = reported_epoch
            reported_backup_address = request.synchronous_backup_address.strip()
            reported_backup = self.nodes.get(reported_backup_address)
            recovered_primary_is_ready = bool(
                request.role == "primary"
                and reported_epoch > 0
                and reported_epoch == self.last_primary_epoch
                and request.promotion_ready
                and reported_backup_address
                and reported_backup_address != address
                and reported_backup is not None
                and reported_backup.sync_status == ReplicaSyncStatus.SYNCHRONIZED
                and reported_backup.synchronized_epoch == reported_epoch
            )
            if (
                self.primary_assignment is None
                and recovered_primary_is_ready
            ):
                self.primary_assignment = PrimaryAssignment(
                    node_id=address,
                    epoch=reported_epoch,
                    status=PrimaryStatus.READY,
                    sync_backup_address=reported_backup_address,
                )
                LOGGER.info("Recovered primary assignment from %s", address)
            elif self.primary_assignment is None and self.last_primary_epoch == 0:
                self.last_primary_epoch += 1
                self.primary_assignment = PrimaryAssignment(
                    node_id=address,
                    epoch=self.last_primary_epoch,
                    status=PrimaryStatus.READY,
                )
                LOGGER.info("Initial replica assigned as primary: %s", address)
            assignment = self.primary_assignment
            if (
                assignment is not None
                and assignment.status == PrimaryStatus.READY
                and assignment.sync_backup_address is None
                and address != assignment.primary_address
            ):
                assignment.status = PrimaryStatus.REPROTECTING
                assignment.attempted_backup_addresses.clear()
                assignment.replacement_candidate_address = None
                reprotection_args = (
                    assignment.primary_address,
                    assignment.epoch,
                )
            self._refresh_gauges_locked()
            LOGGER.info("Registered replica: %s", address)
            response = pb2.RegisterResponse(
                success=True,
                is_primary=bool(
                    assignment and address == assignment.primary_address
                ),
                message="Replica registered successfully",
                epoch=assignment.epoch if assignment else self.last_primary_epoch,
            )
        if reprotection_args is not None:
            threading.Thread(
                target=self._try_next_replacement_backup,
                args=reprotection_args,
                daemon=True,
            ).start()
        return response

    @observe_rpc("controller", "GetPrimary", _classify_success)
    def GetPrimary(self, request, context):
        with self.lock:
            assignment = self.primary_assignment
            if assignment is None:
                return pb2.GetPrimaryResponse(success=False, message="No Primary Judge available")
            if assignment.status != PrimaryStatus.READY:
                return pb2.GetPrimaryResponse(
                    success=False,
                    epoch=assignment.epoch,
                    message="Primary promotion is not complete.",
                )
            return pb2.GetPrimaryResponse(
                success=True,
                primary_address=assignment.primary_address,
                epoch=assignment.epoch,
                message="Primary retrieved",
            )

    @observe_rpc("controller", "GetClusterInfo", _classify_success)
    def GetClusterInfo(self, request, context):
        with self.lock:
            addresses = sorted(self.nodes)
            return pb2.ClusterInfoResponse(
                success=True,
                node_addresses=addresses,
                message=f"Found {len(addresses)} active replicas",
            )

    @observe_rpc("controller", "ReportSynchronizationComplete", _classify_success)
    def ReportSynchronizationComplete(self, request, context):
        replica_address = request.replica_address.strip()
        source_primary_address = request.source_primary_address.strip()
        with self.lock:
            received_assignment = self.primary_assignment
            _log_event(
                logging.INFO,
                "synchronization_completion_received",
                reporting_replica=replica_address or "missing",
                reported_source_primary=source_primary_address or "missing",
                reported_epoch=request.epoch,
                reported_generation="unavailable",
                assignment_status=(
                    received_assignment.status
                    if received_assignment
                    else "missing"
                ),
                expected_primary=(
                    received_assignment.primary_address
                    if received_assignment
                    else None
                ),
                expected_candidate=(
                    received_assignment.replacement_candidate_address
                    or received_assignment.sync_backup_address
                    if received_assignment
                    else None
                ),
                expected_epoch=(
                    received_assignment.epoch if received_assignment else None
                ),
                expected_generation=(
                    received_assignment.recovery_generation
                    if received_assignment
                    else None
                ),
                active_backup=(
                    received_assignment.sync_backup_address
                    if received_assignment
                    else None
                ),
            )
        if not replica_address or not source_primary_address:
            _log_event(
                logging.WARNING,
                "synchronization_completion_rejected",
                reason="wrong_reporting_replica",
                reporting_replica=replica_address or "missing",
                epoch=request.epoch,
            )
            return pb2.SynchronizationCompleteResponse(
                success=False,
                message="Replica and source primary addresses are required.",
            )

        with self.lock:
            replica = self.nodes.get(replica_address)
            assignment = self.primary_assignment
            if replica is None:
                _log_event(
                    logging.WARNING,
                    "synchronization_completion_rejected",
                    reason="candidate_not_healthy",
                    reporting_replica=replica_address,
                    epoch=request.epoch,
                )
                return pb2.SynchronizationCompleteResponse(
                    success=False, message="Replica is not registered."
                )
            if not self._assignment_matches_locked(
                source_primary_address,
                request.epoch,
                assignment.status if assignment else None,
            ):
                if assignment is None:
                    rejection_reason = "assignment_missing"
                elif source_primary_address != assignment.primary_address:
                    rejection_reason = "wrong_source_primary"
                elif request.epoch != assignment.epoch:
                    rejection_reason = "stale_epoch"
                else:
                    rejection_reason = "wrong_state"
                _log_event(
                    logging.WARNING,
                    "synchronization_completion_rejected",
                    reason=rejection_reason,
                    reporting_replica=replica_address,
                    reported_source_primary=source_primary_address,
                    reported_epoch=request.epoch,
                    expected_primary=(
                        assignment.primary_address if assignment else None
                    ),
                    expected_epoch=assignment.epoch if assignment else None,
                )
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="Synchronization source or epoch does not match the current primary.",
                )
            if replica_address == assignment.primary_address:
                _log_event(
                    logging.WARNING,
                    "synchronization_completion_rejected",
                    reason="wrong_reporting_replica",
                    reporting_replica=replica_address,
                    epoch=request.epoch,
                )
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="The primary cannot report itself as a synchronized backup.",
                )
            if assignment.status == PrimaryStatus.READY:
                _log_event(
                    logging.WARNING,
                    "synchronization_completion_rejected",
                    reason="unsolicited_while_ready",
                    reporting_replica=replica_address,
                    epoch=request.epoch,
                )
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="No controller-owned synchronization is in progress.",
                )
            expected_backup_address = (
                assignment.sync_backup_address
                if assignment.status == PrimaryStatus.PROMOTING
                else assignment.replacement_candidate_address
            )
            if replica_address != expected_backup_address:
                _log_event(
                    logging.WARNING,
                    "synchronization_completion_rejected",
                    reason="wrong_reporting_replica",
                    reporting_replica=replica_address,
                    expected_candidate=expected_backup_address,
                    epoch=request.epoch,
                    expected_generation=assignment.recovery_generation,
                )
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="Replica is not the designated synchronization target.",
                )
            recovery_generation = assignment.recovery_generation
            replica.sync_status = ReplicaSyncStatus.SYNCHRONIZED
            replica.synchronized_epoch = assignment.epoch
            candidate_address = assignment.primary_address
            epoch = assignment.epoch
            status = assignment.status
            backup_address = replica_address

        _log_event(
            logging.INFO,
            "synchronization_completion_accepted",
            candidate=backup_address,
            primary=candidate_address,
            epoch=epoch,
            generation=recovery_generation,
            next_stage="backup_activation",
        )
        _log_event(
            logging.INFO,
            "backup_activation_started",
            primary=candidate_address,
            candidate=backup_address,
            epoch=epoch,
            generation=recovery_generation,
            active_backup_before=(
                received_assignment.sync_backup_address
                if received_assignment
                else None
            ),
            owner="controller_reprotection",
        )
        # Storage calls must stay outside the controller lock. The assignment
        # guard below prevents a delayed response from activating stale state.
        try:
            with grpc.insecure_channel(candidate_address) as channel:
                completion = pb2_grpc.StorageReplicaServiceStub(
                    channel
                ).CompletePrimaryPromotion(
                    pb2.CompletePrimaryPromotionRequest(
                        epoch=epoch, backup_address=backup_address
                    ),
                    timeout=self._promotion_timeout,
                )
        except grpc.RpcError as error:
            timed_out = _rpc_status(error) == grpc.StatusCode.DEADLINE_EXCEEDED
            _log_event(
                logging.WARNING,
                (
                    "backup_activation_timeout"
                    if timed_out
                    else "backup_activation_failed"
                ),
                primary=candidate_address,
                candidate=backup_address,
                epoch=epoch,
                generation=recovery_generation,
                exception_type=type(error).__name__,
                retry_planned=False,
                owner="controller_reprotection",
            )
            if status == PrimaryStatus.REPROTECTING:
                _log_event(
                    logging.WARNING,
                    "reprotection_candidate_failed",
                    candidate=backup_address,
                    stage="backup_activation",
                    reason=type(error).__name__,
                    epoch=epoch,
                    generation=recovery_generation,
                    excluded_for_attempt=True,
                    retry_planned=False,
                )
            if status == PrimaryStatus.PROMOTING:
                self._abandon_promotion_attempt(
                    candidate_address,
                    epoch,
                    "completion result is ambiguous",
                    outcome="abandoned",
                )
            else:
                self._reject_replacement_candidate(
                    candidate_address,
                    epoch,
                    backup_address,
                    recovery_generation,
                )
            return pb2.SynchronizationCompleteResponse(
                success=False, message="Primary backup configuration RPC failed."
            )
        except Exception:
            LOGGER.exception(
                "event=backup_activation_failed primary=%s candidate=%s "
                "epoch=%s generation=%s exception_type=unexpected "
                "retry_planned=false owner=controller_reprotection",
                candidate_address,
                backup_address,
                epoch,
                recovery_generation,
            )
            raise

        if not completion.success or completion.epoch != epoch:
            _log_event(
                logging.WARNING,
                "backup_activation_rejected",
                primary=candidate_address,
                candidate=backup_address,
                epoch=epoch,
                generation=recovery_generation,
                response_reason=completion.message or "epoch_mismatch",
                retry_planned=False,
                owner="controller_reprotection",
            )
            if status == PrimaryStatus.REPROTECTING:
                _log_event(
                    logging.WARNING,
                    "reprotection_candidate_failed",
                    candidate=backup_address,
                    stage="backup_activation",
                    reason=completion.message or "epoch_mismatch",
                    epoch=epoch,
                    generation=recovery_generation,
                    excluded_for_attempt=True,
                    retry_planned=False,
                )
            if status == PrimaryStatus.PROMOTING:
                self._handle_candidate_promotion_failure(
                    candidate_address,
                    epoch,
                    "completion rejected",
                    promotion_outcome="rejected",
                )
            else:
                self._reject_replacement_candidate(
                    candidate_address,
                    epoch,
                    backup_address,
                    recovery_generation,
                )
            return pb2.SynchronizationCompleteResponse(
                success=False, message="Primary backup configuration was rejected."
            )
        with self.lock:
            assignment = self.primary_assignment
            expected_still_matches = bool(
                assignment
                and assignment.primary_address == candidate_address
                and assignment.epoch == epoch
                and assignment.status == status
                and assignment.recovery_generation == recovery_generation
                and (
                    (
                        status == PrimaryStatus.PROMOTING
                        and assignment.sync_backup_address == backup_address
                    )
                    or (
                        status == PrimaryStatus.REPROTECTING
                        and assignment.replacement_candidate_address
                        == backup_address
                    )
                )
            )
            if not expected_still_matches:
                _log_event(
                    logging.WARNING,
                    "synchronization_completion_rejected",
                    reason="recovery_attempt_replaced",
                    reporting_replica=backup_address,
                    epoch=epoch,
                    expected_generation=(
                        assignment.recovery_generation if assignment else None
                    ),
                )
                return pb2.SynchronizationCompleteResponse(
                    success=False, message="Primary assignment changed during completion."
                )
            assignment.sync_backup_address = backup_address
            assignment.replacement_candidate_address = None
            assignment.status = PrimaryStatus.READY
            if status == PrimaryStatus.PROMOTING:
                self._finish_promotion_locked(candidate_address, epoch, "completed")
                self._finish_failover_locked("completed")
            self._refresh_gauges_locked()
        _log_event(
            logging.INFO,
            "backup_activation_succeeded",
            primary=candidate_address,
            candidate=backup_address,
            epoch=epoch,
            generation=recovery_generation,
            response_reason=completion.message or "accepted",
            retry_planned=False,
            owner="controller_reprotection",
        )
        if status == PrimaryStatus.REPROTECTING:
            _log_event(
                logging.INFO,
                "reprotection_completed",
                primary=candidate_address,
                active_backup=backup_address,
                epoch=epoch,
                generation=recovery_generation,
                cluster_ready=True,
            )
        return pb2.SynchronizationCompleteResponse(
            success=True,
            message="Replica synchronized and primary backup configured.",
        )

    # Assignment and election helpers

    def _assignment_matches_locked(
        self,
        primary_address: str,
        epoch: int,
        status: PrimaryStatus | None,
        backup_address: str | None = None,
    ) -> bool:
        assignment = self.primary_assignment
        return bool(
            assignment
            and assignment.primary_address == primary_address
            and assignment.epoch == epoch
            and (status is None or assignment.status == status)
            and (
                backup_address is None
                or assignment.sync_backup_address == backup_address
            )
        )

    def _select_primary_candidate_locked(self, eligible_addresses=None) -> str | None:
        if eligible_addresses is None:
            assignment_epoch = (
                self.primary_assignment.epoch
                if self.primary_assignment
                else self.last_primary_epoch
            )
            eligible_addresses = (
                address
                for address, replica in self.nodes.items()
                if replica.promotion_eligible
                and replica.synchronized_epoch in (0, assignment_epoch)
            )
        return next(
            (
                address
                for address in sorted(eligible_addresses)
                if address in self.nodes
            ),
            None,
        )

    def _select_backup_locked(
        self, candidate_address: str, assignment: PrimaryAssignment
    ) -> str | None:
        eligible = assignment.eligible_backup_addresses or tuple(
            address
            for address, replica in self.nodes.items()
            if replica.promotion_eligible
        )
        return next(
            (
                address
                for address in sorted(eligible)
                if address != candidate_address
                and address in self.nodes
                and address not in assignment.attempted_backup_addresses
            ),
            None,
        )

    def _select_replacement_backup_locked(
        self, assignment: PrimaryAssignment
    ) -> str | None:
        return next(
            (
                address
                for address in sorted(self.nodes)
                if address != assignment.primary_address
                and address not in assignment.attempted_backup_addresses
                and self.nodes[address].consecutive_heartbeat_failures == 0
            ),
            None,
        )

    def _reject_replacement_candidate(
        self,
        primary_address: str,
        epoch: int,
        backup_address: str,
        recovery_generation: int,
    ) -> None:
        with self.lock:
            assignment = self.primary_assignment
            if not (
                assignment
                and assignment.primary_address == primary_address
                and assignment.epoch == epoch
                and assignment.status == PrimaryStatus.REPROTECTING
                and assignment.replacement_candidate_address == backup_address
                and assignment.recovery_generation == recovery_generation
            ):
                return
            assignment.replacement_candidate_address = None
            replica = self.nodes.get(backup_address)
            if replica is not None:
                replica.sync_status = ReplicaSyncStatus.UNSYNCHRONIZED
                replica.synchronized_epoch = 0
            self._refresh_gauges_locked()

    def _try_next_replacement_backup(self, primary_address: str, epoch: int):
        with self.lock:
            if not self._assignment_matches_locked(
                primary_address, epoch, PrimaryStatus.REPROTECTING
            ):
                assignment = self.primary_assignment
                _log_event(
                    logging.WARNING,
                    "reprotection_entry_rejected",
                    reason=(
                        "assignment_missing"
                        if assignment is None
                        else "stale_assignment"
                    ),
                    primary=primary_address,
                    epoch=epoch,
                    recovery_generation=(
                        assignment.recovery_generation if assignment else 0
                    ),
                )
                return
            assignment = self.primary_assignment
            healthy_nodes = sorted(
                address
                for address, node in self.nodes.items()
                if node.consecutive_heartbeat_failures == 0
            )
            eligible_candidates = [
                address
                for address in healthy_nodes
                if address != assignment.primary_address
                and address not in assignment.attempted_backup_addresses
            ]
            excluded_candidates = sorted(
                set(self.nodes) - set(eligible_candidates) - {primary_address}
            )
            _log_event(
                logging.INFO,
                "reprotection_candidates_evaluated",
                primary=primary_address,
                healthy_nodes=healthy_nodes,
                eligible_candidates=eligible_candidates,
                excluded_candidates=excluded_candidates,
                active_backup=assignment.sync_backup_address,
                previous_failed_candidates=sorted(
                    assignment.attempted_backup_addresses
                ),
                epoch=epoch,
                recovery_generation=assignment.recovery_generation,
            )
            backup_address = self._select_replacement_backup_locked(assignment)
            if backup_address is None:
                _log_event(
                    logging.WARNING,
                    "reprotection_candidate_unavailable",
                    primary=primary_address,
                    healthy_nodes=healthy_nodes,
                    eligible_candidates=eligible_candidates,
                    excluded_candidates=excluded_candidates,
                    active_backup=assignment.sync_backup_address,
                    previous_failed_candidates=sorted(
                        assignment.attempted_backup_addresses
                    ),
                    epoch=epoch,
                    recovery_generation=assignment.recovery_generation,
                )
                self._refresh_gauges_locked()
                return
            old_generation = assignment.recovery_generation
            previous_failed = sorted(assignment.attempted_backup_addresses)
            assignment.attempted_backup_addresses.add(backup_address)
            assignment.recovery_generation += 1
            recovery_generation = assignment.recovery_generation
            assignment.replacement_candidate_address = backup_address
            backup = self.nodes[backup_address]
            backup.sync_status = ReplicaSyncStatus.UNSYNCHRONIZED
            backup.synchronized_epoch = 0
            remaining_candidates = [
                address
                for address in eligible_candidates
                if address != backup_address
            ]
            _log_event(
                logging.INFO,
                "reprotection_candidate_selected",
                candidate=backup_address,
                primary=primary_address,
                epoch=epoch,
                recovery_generation=recovery_generation,
                remaining_candidates=remaining_candidates,
            )
            if previous_failed:
                _log_event(
                    logging.WARNING,
                    "reprotection_retry_started",
                    primary=primary_address,
                    previous_candidate=previous_failed[-1],
                    next_candidate=backup_address,
                    epoch=epoch,
                    old_generation=old_generation,
                    new_generation=recovery_generation,
                )

        synchronization_started_at = time.perf_counter()
        _log_event(
            logging.INFO,
            "synchronization_dispatch_started",
            source_primary=primary_address,
            target_candidate=backup_address,
            epoch=epoch,
            recovery_generation=recovery_generation,
            rpc_timeout=self._synchronization_timeout,
        )
        try:
            with grpc.insecure_channel(backup_address) as channel:
                synchronization = pb2_grpc.StorageReplicaServiceStub(
                    channel
                ).SynchronizeFromPrimary(
                    pb2.SynchronizeFromPrimaryRequest(
                        primary_address=primary_address, epoch=epoch
                    ),
                    timeout=self._synchronization_timeout,
                )
        except grpc.RpcError as error:
            timed_out = _rpc_status(error) == grpc.StatusCode.DEADLINE_EXCEEDED
            record_synchronization(
                "timeout" if timed_out else "failed",
                time.perf_counter() - synchronization_started_at,
            )
            _log_event(
                logging.WARNING,
                (
                    "synchronization_dispatch_timeout"
                    if timed_out
                    else "synchronization_dispatch_failed"
                ),
                source_primary=primary_address,
                target_candidate=backup_address,
                epoch=epoch,
                recovery_generation=recovery_generation,
                exception_type=type(error).__name__,
                candidate_retry_planned=True,
            )
            _log_event(
                logging.WARNING,
                "reprotection_candidate_failed",
                candidate=backup_address,
                stage="dispatch",
                reason=type(error).__name__,
                epoch=epoch,
                generation=recovery_generation,
                excluded_for_attempt=True,
                retry_planned=True,
            )
            with self.lock:
                if self._assignment_matches_locked(
                    primary_address,
                    epoch,
                    PrimaryStatus.REPROTECTING,
                ):
                    assignment = self.primary_assignment
                    if (
                        assignment.replacement_candidate_address == backup_address
                        and assignment.recovery_generation == recovery_generation
                    ):
                        assignment.replacement_candidate_address = None
            self._try_next_replacement_backup(primary_address, epoch)
            return
        except Exception:
            record_synchronization(
                "failed", time.perf_counter() - synchronization_started_at
            )
            LOGGER.exception(
                "event=synchronization_dispatch_failed source_primary=%s "
                "target_candidate=%s epoch=%s recovery_generation=%s "
                "exception_type=unexpected candidate_retry_planned=true",
                primary_address,
                backup_address,
                epoch,
                recovery_generation,
            )
            _log_event(
                logging.WARNING,
                "reprotection_candidate_failed",
                candidate=backup_address,
                stage="dispatch",
                reason="unexpected_exception",
                epoch=epoch,
                generation=recovery_generation,
                excluded_for_attempt=True,
                retry_planned=True,
            )
            with self.lock:
                if self._assignment_matches_locked(
                    primary_address,
                    epoch,
                    PrimaryStatus.REPROTECTING,
                ):
                    assignment = self.primary_assignment
                    if (
                        assignment.replacement_candidate_address == backup_address
                        and assignment.recovery_generation == recovery_generation
                    ):
                        assignment.replacement_candidate_address = None
            self._try_next_replacement_backup(primary_address, epoch)
            return
        if not synchronization.success or synchronization.epoch != epoch:
            record_synchronization(
                "rejected", time.perf_counter() - synchronization_started_at
            )
            _log_event(
                logging.WARNING,
                "synchronization_dispatch_rejected",
                source_primary=primary_address,
                target_candidate=backup_address,
                epoch=epoch,
                recovery_generation=recovery_generation,
                response_reason=synchronization.message or "epoch_mismatch",
                candidate_retry_planned=True,
            )
            _log_event(
                logging.WARNING,
                "reprotection_candidate_failed",
                candidate=backup_address,
                stage="dispatch",
                reason=synchronization.message or "epoch_mismatch",
                epoch=epoch,
                generation=recovery_generation,
                excluded_for_attempt=True,
                retry_planned=True,
            )
            with self.lock:
                if self._assignment_matches_locked(
                    primary_address,
                    epoch,
                    PrimaryStatus.REPROTECTING,
                ):
                    assignment = self.primary_assignment
                    if (
                        assignment.replacement_candidate_address == backup_address
                        and assignment.recovery_generation == recovery_generation
                    ):
                        assignment.replacement_candidate_address = None
            self._try_next_replacement_backup(primary_address, epoch)
            return
        record_synchronization(
            "completed", time.perf_counter() - synchronization_started_at
        )
        _log_event(
            logging.INFO,
            "synchronization_dispatch_accepted",
            source_primary=primary_address,
            target_candidate=backup_address,
            epoch=epoch,
            recovery_generation=recovery_generation,
            response_reason=synchronization.message or "accepted",
            candidate_retry_planned=False,
        )

    def _elect_new_primary(self, eligible_addresses=None):
        with self.lock:
            if self.primary_assignment is not None or self._election_in_progress:
                return
            if self._failover_started_at is None and self.last_primary_epoch > 0:
                self._failover_started_at = time.perf_counter()
            candidate_address = self._select_primary_candidate_locked(eligible_addresses)
            if candidate_address is None:
                LOGGER.critical("No synchronized replica can be promoted")
                self._finish_failover_locked("failed")
                self._refresh_gauges_locked()
                return
            prior_eligible = tuple(self.nodes)
            self.last_primary_epoch += 1
            assignment = PrimaryAssignment(
                node_id=candidate_address,
                epoch=self.last_primary_epoch,
                status=PrimaryStatus.PROMOTING,
                eligible_backup_addresses=prior_eligible,
            )
            self.primary_assignment = assignment
            self._election_in_progress = True
            self._promotion_started_at[(candidate_address, assignment.epoch)] = (
                time.perf_counter()
            )
            for replica in self.nodes.values():
                replica.sync_status = ReplicaSyncStatus.UNSYNCHRONIZED
                replica.synchronized_epoch = 0
            self._refresh_gauges_locked()
        LOGGER.info(
            "Elected primary candidate %s for epoch %s",
            candidate_address,
            assignment.epoch,
        )
        threading.Thread(
            target=self._notify_promotion,
            args=(candidate_address, assignment.epoch),
            daemon=True,
        ).start()

    # Promotion workflow

    def _validate_promotion_context(self, candidate_address: str, epoch: int) -> bool:
        with self.lock:
            assignment = self.primary_assignment
            replica = self.nodes.get(candidate_address)
            return bool(
                self._assignment_matches_locked(
                    candidate_address, epoch, PrimaryStatus.PROMOTING
                )
                and replica is not None
                and (
                    replica.promotion_eligible
                    or candidate_address in assignment.eligible_backup_addresses
                )
            )

    def _notify_promotion(self, candidate_address, epoch):
        if not self._validate_promotion_context(candidate_address, epoch):
            return
        try:
            with grpc.insecure_channel(candidate_address) as channel:
                stub = pb2_grpc.StorageReplicaServiceStub(channel)
                begun = stub.BeginPrimaryPromotion(
                    pb2.BeginPrimaryPromotionRequest(epoch=epoch),
                    timeout=self._promotion_timeout,
                )
                if not begun.accepted or begun.epoch != epoch:
                    self._handle_candidate_promotion_failure(
                        candidate_address,
                        epoch,
                        "begin rejected",
                        promotion_outcome="rejected",
                    )
                    return
                if not self._validate_promotion_context(candidate_address, epoch):
                    return
                confirmed = stub.ConfirmPromotionState(
                    pb2.PromotionStateConfirmationRequest(epoch=epoch),
                    timeout=self._promotion_timeout,
                )
            if not confirmed.confirmed or confirmed.epoch != epoch:
                self._handle_candidate_promotion_failure(
                    candidate_address,
                    epoch,
                    "state confirmation rejected",
                    promotion_outcome="rejected",
                )
                return
            self._try_next_promotion_backup(candidate_address, epoch)
        except grpc.RpcError as error:
            LOGGER.warning(
                "Promotion RPC for %s at epoch %s failed: %s",
                candidate_address,
                epoch,
                error,
            )
            self._handle_candidate_promotion_failure(
                candidate_address,
                epoch,
                "candidate promotion RPC failed",
                promotion_outcome=(
                    "timeout"
                    if _rpc_status(error) == grpc.StatusCode.DEADLINE_EXCEEDED
                    else "failed"
                ),
            )
        except Exception:
            LOGGER.exception(
                "Unexpected error promoting %s at epoch %s",
                candidate_address,
                epoch,
            )
            self._handle_candidate_promotion_failure(
                candidate_address,
                epoch,
                "unexpected candidate promotion error",
                promotion_outcome="failed",
            )

    def _try_next_promotion_backup(self, candidate_address: str, epoch: int):
        with self.lock:
            if not self._assignment_matches_locked(
                candidate_address, epoch, PrimaryStatus.PROMOTING
            ):
                return
            assignment = self.primary_assignment
            backup_address = self._select_backup_locked(
                candidate_address, assignment
            )
            if backup_address is None:
                exhausted = True
            else:
                exhausted = False
                assignment.attempted_backup_addresses.add(backup_address)
                assignment.sync_backup_address = backup_address
                backup = self.nodes[backup_address]
                backup.sync_status = ReplicaSyncStatus.UNSYNCHRONIZED
                backup.synchronized_epoch = 0
        if exhausted:
            self._abandon_promotion_attempt(
                candidate_address, epoch, "no eligible promotion backup"
            )
            return
        synchronization_started_at = time.perf_counter()
        try:
            with grpc.insecure_channel(backup_address) as channel:
                synchronization = pb2_grpc.StorageReplicaServiceStub(
                    channel
                ).SynchronizeFromPrimary(
                    pb2.SynchronizeFromPrimaryRequest(
                        primary_address=candidate_address, epoch=epoch
                    ),
                    timeout=self._synchronization_timeout,
                )
        except grpc.RpcError as error:
            record_synchronization(
                "timeout"
                if _rpc_status(error) == grpc.StatusCode.DEADLINE_EXCEEDED
                else "failed",
                time.perf_counter() - synchronization_started_at,
            )
            LOGGER.warning(
                "Synchronization RPC to backup %s failed: %s",
                backup_address,
                error,
            )
            self._handle_promotion_backup_failure(
                candidate_address, epoch, backup_address, "synchronization RPC failed"
            )
            return
        except Exception:
            record_synchronization(
                "failed", time.perf_counter() - synchronization_started_at
            )
            LOGGER.exception(
                "Unexpected synchronization error for backup %s", backup_address
            )
            self._handle_promotion_backup_failure(
                candidate_address, epoch, backup_address, "unexpected synchronization error"
            )
            return
        if not synchronization.success or synchronization.epoch != epoch:
            record_synchronization(
                "rejected", time.perf_counter() - synchronization_started_at
            )
            self._handle_promotion_backup_failure(
                candidate_address, epoch, backup_address, "synchronization rejected"
            )
            return
        record_synchronization(
            "completed", time.perf_counter() - synchronization_started_at
        )
        LOGGER.info(
                "Backup %s synchronized from %s for epoch %s",
            backup_address,
            candidate_address,
            epoch,
        )

    # Promotion recovery

    def _handle_candidate_promotion_failure(
        self,
        candidate_address: str,
        epoch: int,
        reason: str,
        *,
        promotion_outcome: str = "failed",
    ):
        with self.lock:
            if not self._assignment_matches_locked(
                candidate_address, epoch, PrimaryStatus.PROMOTING
            ):
                return
            assignment = self.primary_assignment
            retry_addresses = tuple(
                address
                for address in assignment.eligible_backup_addresses
                if address != candidate_address and address in self.nodes
            )
            removed = self.nodes.pop(candidate_address, None)
            if removed is not None:
                record_health_transition("removed")
            self._finish_promotion_locked(
                candidate_address, epoch, promotion_outcome
            )
            for replica in self.nodes.values():
                replica.sync_status = ReplicaSyncStatus.UNSYNCHRONIZED
                replica.synchronized_epoch = 0
            self.primary_assignment = None
            self._election_in_progress = False
            self._refresh_gauges_locked()
        LOGGER.warning(
            "Promotion of %s at epoch %s failed (%s)",
            candidate_address,
            epoch,
            reason,
        )
        self._elect_new_primary(retry_addresses)

    def _handle_promotion_backup_failure(
        self,
        candidate_address: str,
        epoch: int,
        backup_address: str,
        reason: str,
    ):
        with self.lock:
            if not self._assignment_matches_locked(
                candidate_address,
                epoch,
                PrimaryStatus.PROMOTING,
                backup_address,
            ):
                return
            removed = self.nodes.pop(backup_address, None)
            if removed is not None:
                record_health_transition("removed")
            self.primary_assignment.sync_backup_address = None
            self._refresh_gauges_locked()
        LOGGER.warning(
            "Promotion backup %s failed for candidate %s at epoch %s (%s)",
            backup_address,
            candidate_address,
            epoch,
            reason,
        )
        self._try_next_promotion_backup(candidate_address, epoch)

    def _abandon_promotion_attempt(
        self,
        candidate_address: str,
        epoch: int,
        reason: str,
        *,
        outcome: str = "failed",
    ):
        with self.lock:
            if not self._assignment_matches_locked(
                candidate_address, epoch, PrimaryStatus.PROMOTING
            ):
                return
            for replica in self.nodes.values():
                replica.sync_status = ReplicaSyncStatus.UNSYNCHRONIZED
                replica.synchronized_epoch = 0
            self.primary_assignment = None
            self._election_in_progress = False
            self._finish_promotion_locked(candidate_address, epoch, outcome)
            self._finish_failover_locked(outcome)
            self._refresh_gauges_locked()
        LOGGER.warning(
            "Abandoned promotion of %s at epoch %s (%s)",
            candidate_address,
            epoch,
            reason,
        )

    # Heartbeat monitoring and replica eviction

    def _monitor_heartbeats(self):
        while True:
            time.sleep(5)
            self._check_heartbeats_once()

    def _check_heartbeats_once(self) -> None:
        """Probe the current membership once.

        Keeping one pass separate makes the production heartbeat behavior
        testable with real gRPC servers without starting an immortal monitor
        thread in the test process.
        """
        with self.lock:
            # Snapshot membership before making any network calls.
            addresses = sorted(self.nodes)
        for address in addresses:
            try:
                with grpc.insecure_channel(address) as channel:
                    response = pb2_grpc.StorageReplicaServiceStub(
                        channel
                    ).Heartbeat(
                        pb2.HealthCheckRequest(request_source="CONTROLLER"),
                        timeout=self._heartbeat_timeout,
                    )
            except grpc.RpcError as error:
                LOGGER.warning("Heartbeat RPC to %s failed: %s", address, error)
                self._record_heartbeat_failure(address)
                continue
            except Exception:
                LOGGER.exception("Unexpected heartbeat error for %s", address)
                self._record_heartbeat_failure(address)
                continue
            if not response.alive:
                LOGGER.warning("Replica %s reported an unhealthy status", address)
                self._record_heartbeat_failure(address)
                continue
            with self.lock:
                # The replica may have been evicted while its heartbeat ran.
                replica = self.nodes.get(address)
                if replica is not None:
                    was_unhealthy = replica.consecutive_heartbeat_failures > 0
                    replica.last_seen = time.time()
                    replica.consecutive_heartbeat_failures = 0
                    if was_unhealthy:
                        record_health_transition("unhealthy_to_healthy")
                    self._refresh_gauges_locked()

    def _record_heartbeat_failure(self, address: str) -> None:
        """Evict only after a short run of failed health checks."""
        with self.lock:
            replica = self.nodes.get(address)
            if replica is None:
                return
            was_healthy = replica.consecutive_heartbeat_failures == 0
            replica.consecutive_heartbeat_failures += 1
            if was_healthy:
                record_health_transition("healthy_to_unhealthy")
                self._refresh_gauges_locked()
            should_evict = replica.consecutive_heartbeat_failures >= 3
        if should_evict:
            self._handle_replica_failure(address)

    def _handle_replica_failure(self, address: str):
        should_elect = False
        should_reprotect = False
        entered_reprotecting = False
        replacement_worker_started = False
        branch_taken = "unassigned_replica"
        primary_eligible_addresses = None
        candidate_address = None
        epoch = 0
        with self.lock:
            if self.nodes.pop(address, None) is None:
                return
            record_health_transition("removed")
            assignment = self.primary_assignment
            if assignment is None:
                classification = "unassigned_replica"
            elif assignment.primary_address == address:
                classification = "primary"
            elif assignment.sync_backup_address == address:
                classification = "active_backup"
            elif assignment.replacement_candidate_address == address:
                classification = "recovery_candidate"
            else:
                classification = "unassigned_replica"
            _log_event(
                logging.WARNING,
                "replica_failure_classified",
                failed_replica=address,
                classification=classification,
                assignment_status=assignment.status if assignment else "missing",
                primary=assignment.primary_address if assignment else None,
                active_backup=assignment.sync_backup_address if assignment else None,
                recovery_candidate=(
                    assignment.replacement_candidate_address
                    if assignment
                    else None
                ),
                epoch=assignment.epoch if assignment else self.last_primary_epoch,
                recovery_generation=(
                    assignment.recovery_generation if assignment else 0
                ),
            )
            if assignment is None:
                should_elect = not self._election_in_progress
                branch_taken = "elect_without_assignment"
            elif assignment.primary_address == address:
                if (
                    assignment.sync_backup_address
                    and assignment.sync_backup_address in self.nodes
                ):
                    primary_eligible_addresses = (
                        assignment.sync_backup_address,
                    )
                self.primary_assignment = None
                self._election_in_progress = False
                should_elect = True
                branch_taken = "primary_failure"
            elif assignment.sync_backup_address == address:
                if assignment.status == PrimaryStatus.PROMOTING:
                    candidate_address = assignment.primary_address
                    epoch = assignment.epoch
                    branch_taken = "promotion_backup_failure"
                elif assignment.status == PrimaryStatus.READY:
                    previous_status = assignment.status
                    assignment.sync_backup_address = None
                    assignment.status = PrimaryStatus.REPROTECTING
                    assignment.attempted_backup_addresses.clear()
                    candidate_address = assignment.primary_address
                    epoch = assignment.epoch
                    should_reprotect = True
                    entered_reprotecting = True
                    branch_taken = "active_backup_failure"
                    _log_event(
                        logging.INFO,
                        "reprotection_entered",
                        trigger="active_backup_failure",
                        failed_backup=address,
                        primary=candidate_address,
                        previous_status=previous_status,
                        epoch=epoch,
                        recovery_generation=assignment.recovery_generation,
                        cluster_ready=False,
                    )
                else:
                    branch_taken = "active_backup_wrong_status"
                    _log_event(
                        logging.WARNING,
                        "reprotection_entry_rejected",
                        reason="wrong_status",
                        failed_replica=address,
                        primary=assignment.primary_address,
                        epoch=assignment.epoch,
                        recovery_generation=assignment.recovery_generation,
                    )
            elif (
                assignment.status == PrimaryStatus.REPROTECTING
                and assignment.replacement_candidate_address == address
            ):
                assignment.replacement_candidate_address = None
                candidate_address = assignment.primary_address
                epoch = assignment.epoch
                should_reprotect = True
                branch_taken = "recovery_candidate_failure"
            self._refresh_gauges_locked()
            resulting_assignment = self.primary_assignment
        LOGGER.warning("Evicted failed replica %s", address)
        if should_elect:
            self._elect_new_primary(primary_eligible_addresses)
        elif should_reprotect:
            replacement_worker_started = True
            threading.Thread(
                target=self._try_next_replacement_backup,
                args=(candidate_address, epoch),
                daemon=True,
            ).start()
        elif candidate_address:
            self._handle_promotion_backup_failure(
                candidate_address, epoch, address, "selected backup heartbeat failed"
            )
        with self.lock:
            resulting_assignment = self.primary_assignment
            _log_event(
                logging.WARNING,
                "replica_failure_handled",
                failed_replica=address,
                branch_taken=branch_taken,
                entered_reprotecting=entered_reprotecting,
                replacement_worker_started=replacement_worker_started,
                resulting_status=(
                    resulting_assignment.status
                    if resulting_assignment
                    else "missing"
                ),
                resulting_active_backup=(
                    resulting_assignment.sync_backup_address
                    if resulting_assignment
                    else None
                ),
                resulting_recovery_candidate=(
                    resulting_assignment.replacement_candidate_address
                    if resulting_assignment
                    else None
                ),
                epoch=(
                    resulting_assignment.epoch
                    if resulting_assignment
                    else self.last_primary_epoch
                ),
                recovery_generation=(
                    resulting_assignment.recovery_generation
                    if resulting_assignment
                    else 0
                ),
            )
