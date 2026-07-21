from dataclasses import dataclass, field
from enum import Enum
import logging
import threading
import time

import grpc

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc


LOGGER = logging.getLogger(__name__)

HEARTBEAT_RPC_TIMEOUT_SECONDS = 2.0
PROMOTION_RPC_TIMEOUT_SECONDS = 5.0
SYNCHRONIZATION_RPC_TIMEOUT_SECONDS = 30.0


class ReplicaSyncStatus(Enum):
    UNSYNCHRONIZED = "unsynchronized"
    SYNCHRONIZED = "synchronized"


class PrimaryStatus(Enum):
    PROMOTING = "promoting"
    READY = "ready"


@dataclass
class PrimaryAssignment:
    # node_id remains the stored field for compatibility with existing callers.
    node_id: str
    epoch: int
    status: PrimaryStatus
    sync_backup_address: str | None = None
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

    # Public controller RPCs

    def RegisterNode(self, request, context):
        address = request.address.strip()
        if not address:
            return pb2.RegisterResponse(
                success=False,
                message="Replica address is required.",
            )
        with self.lock:
            self.nodes[address] = ReplicaRecord(
                address=address,
                last_seen=time.time(),
                sync_status=ReplicaSyncStatus.UNSYNCHRONIZED,
            )
            if self.primary_assignment is None and self.last_primary_epoch == 0:
                self.last_primary_epoch += 1
                self.primary_assignment = PrimaryAssignment(
                    node_id=address,
                    epoch=self.last_primary_epoch,
                    status=PrimaryStatus.READY,
                )
                LOGGER.info("Initial replica assigned as primary: %s", address)
            assignment = self.primary_assignment
            LOGGER.info("Registered replica: %s", address)
            return pb2.RegisterResponse(
                success=True,
                is_primary=bool(
                    assignment and address == assignment.primary_address
                ),
                message="Replica registered successfully",
                epoch=assignment.epoch if assignment else self.last_primary_epoch,
            )

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

    def GetClusterInfo(self, request, context):
        with self.lock:
            addresses = sorted(self.nodes)
            return pb2.ClusterInfoResponse(
                success=True,
                node_addresses=addresses,
                message=f"Found {len(addresses)} active replicas",
            )

    def ReportSynchronizationComplete(self, request, context):
        replica_address = request.replica_address.strip()
        source_primary_address = request.source_primary_address.strip()
        if not replica_address or not source_primary_address:
            return pb2.SynchronizationCompleteResponse(
                success=False,
                message="Replica and source primary addresses are required.",
            )

        with self.lock:
            replica = self.nodes.get(replica_address)
            assignment = self.primary_assignment
            if replica is None:
                return pb2.SynchronizationCompleteResponse(
                    success=False, message="Replica is not registered."
                )
            if not self._assignment_matches_locked(
                source_primary_address, request.epoch, assignment.status if assignment else None
            ):
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="Synchronization source or epoch does not match the current primary.",
                )
            if replica_address == assignment.primary_address:
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="The primary cannot report itself as a synchronized backup.",
                )
            if (
                assignment.status == PrimaryStatus.PROMOTING
                and replica_address != assignment.sync_backup_address
            ):
                return pb2.SynchronizationCompleteResponse(
                    success=False,
                    message="Replica is not the designated promotion backup.",
                )
            replica.sync_status = ReplicaSyncStatus.SYNCHRONIZED
            replica.synchronized_epoch = assignment.epoch
            if assignment.status == PrimaryStatus.READY:
                return pb2.SynchronizationCompleteResponse(
                    success=True, message="Replica synchronization recorded."
                )
            candidate_address = assignment.primary_address
            epoch = assignment.epoch
            backup_address = assignment.sync_backup_address

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
            LOGGER.warning(
                "CompletePrimaryPromotion RPC to %s failed: %s",
                candidate_address,
                error,
            )
            self._abandon_promotion_attempt(
                candidate_address, epoch, "completion result is ambiguous"
            )
            return pb2.SynchronizationCompleteResponse(
                success=False, message="Primary promotion completion RPC failed."
            )
        except Exception:
            LOGGER.exception(
                "Unexpected error completing promotion of %s", candidate_address
            )
            raise

        if not completion.success or completion.epoch != epoch:
            self._handle_candidate_promotion_failure(
                candidate_address, epoch, "completion rejected"
            )
            return pb2.SynchronizationCompleteResponse(
                success=False, message="Primary promotion completion was rejected."
            )
        with self.lock:
            if not self._assignment_matches_locked(
                candidate_address, epoch, PrimaryStatus.PROMOTING, backup_address
            ):
                return pb2.SynchronizationCompleteResponse(
                    success=False, message="Primary assignment changed during completion."
                )
            self.primary_assignment.status = PrimaryStatus.READY
        return pb2.SynchronizationCompleteResponse(
            success=True,
            message="Replica synchronized and primary promotion completed.",
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

    def _elect_new_primary(self, eligible_addresses=None):
        with self.lock:
            if self.primary_assignment is not None or self._election_in_progress:
                return
            candidate_address = self._select_primary_candidate_locked(eligible_addresses)
            if candidate_address is None:
                LOGGER.critical("No synchronized replica can be promoted")
                return
            prior_eligible = tuple(
                address
                for address, replica in self.nodes.items()
                if replica.promotion_eligible
            )
            if eligible_addresses is not None:
                prior_eligible = tuple(eligible_addresses)
            self.last_primary_epoch += 1
            assignment = PrimaryAssignment(
                node_id=candidate_address,
                epoch=self.last_primary_epoch,
                status=PrimaryStatus.PROMOTING,
                eligible_backup_addresses=prior_eligible,
            )
            self.primary_assignment = assignment
            self._election_in_progress = True
            for replica in self.nodes.values():
                replica.sync_status = ReplicaSyncStatus.UNSYNCHRONIZED
                replica.synchronized_epoch = 0
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
                        candidate_address, epoch, "begin rejected"
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
                    candidate_address, epoch, "state confirmation rejected"
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
                candidate_address, epoch, "candidate promotion RPC failed"
            )
        except Exception:
            LOGGER.exception(
                "Unexpected error promoting %s at epoch %s",
                candidate_address,
                epoch,
            )
            self._handle_candidate_promotion_failure(
                candidate_address, epoch, "unexpected candidate promotion error"
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
            LOGGER.exception(
                "Unexpected synchronization error for backup %s", backup_address
            )
            self._handle_promotion_backup_failure(
                candidate_address, epoch, backup_address, "unexpected synchronization error"
            )
            return
        if not synchronization.success or synchronization.epoch != epoch:
            self._handle_promotion_backup_failure(
                candidate_address, epoch, backup_address, "synchronization rejected"
            )
            return
        LOGGER.info(
                "Backup %s synchronized from %s for epoch %s",
            backup_address,
            candidate_address,
            epoch,
        )

    # Promotion recovery

    def _handle_candidate_promotion_failure(
        self, candidate_address: str, epoch: int, reason: str
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
            self.nodes.pop(candidate_address, None)
            for replica in self.nodes.values():
                replica.sync_status = ReplicaSyncStatus.UNSYNCHRONIZED
                replica.synchronized_epoch = 0
            self.primary_assignment = None
            self._election_in_progress = False
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
            self.nodes.pop(backup_address, None)
            self.primary_assignment.sync_backup_address = None
        LOGGER.warning(
            "Promotion backup %s failed for candidate %s at epoch %s (%s)",
            backup_address,
            candidate_address,
            epoch,
            reason,
        )
        self._try_next_promotion_backup(candidate_address, epoch)

    def _abandon_promotion_attempt(
        self, candidate_address: str, epoch: int, reason: str
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
                    self._handle_replica_failure(address)
                    continue
                except Exception:
                    LOGGER.exception("Unexpected heartbeat error for %s", address)
                    continue
                if not response.alive:
                    LOGGER.warning("Replica %s reported an unhealthy status", address)
                    self._handle_replica_failure(address)
                    continue
                with self.lock:
                    # The replica may have been evicted while its heartbeat ran.
                    replica = self.nodes.get(address)
                    if replica is not None:
                        replica.last_seen = time.time()

    def _handle_replica_failure(self, address: str):
        should_elect = False
        candidate_address = None
        epoch = 0
        with self.lock:
            if self.nodes.pop(address, None) is None:
                return
            assignment = self.primary_assignment
            if assignment is None:
                should_elect = not self._election_in_progress
            elif assignment.primary_address == address:
                self.primary_assignment = None
                self._election_in_progress = False
                should_elect = True
            elif assignment.sync_backup_address == address:
                if assignment.status == PrimaryStatus.PROMOTING:
                    candidate_address = assignment.primary_address
                    epoch = assignment.epoch
        LOGGER.warning("Evicted failed replica %s", address)
        if should_elect:
            self._elect_new_primary()
        elif candidate_address:
            self._handle_promotion_backup_failure(
                candidate_address, epoch, address, "selected backup heartbeat failed"
            )
