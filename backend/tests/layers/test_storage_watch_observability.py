from unittest import TestCase, mock

from prometheus_client import CollectorRegistry, Counter, Gauge

from blindsided.auction_service.service import AuctionService, PrimaryAssignment
from blindsided.generated import blindsided_pb2 as pb2
from blindsided.observability import instrumentation
from backend.tests.helpers import ChannelContext, NoopContext, make_judge


class InactiveContext:
    def is_active(self):
        return False


class WatchService(AuctionService):
    def __init__(self, response=None, error=None):
        self.read_timeout = 1.0
        self.response = response
        self.error = error

    def _resolve_ready_primary(self, context, allow_recovery=False):
        if self.error:
            raise self.error
        return PrimaryAssignment("primary:1", 7)

    def _create_storage_stub(self, address):
        stub = mock.Mock()
        stub.GetAuction.return_value = self.response
        return stub, ChannelContext()


class StorageWatchObservabilityTests(TestCase):
    def setUp(self):
        self.registry = CollectorRegistry()
        collectors = {
            "STORAGE_ROLE": Gauge(
                "blindsided_storage_role", "role", ["role"], registry=self.registry
            ),
            "STORAGE_READY": Gauge(
                "blindsided_storage_ready", "ready", registry=self.registry
            ),
            "STORAGE_EPOCH": Gauge(
                "blindsided_storage_epoch", "epoch", registry=self.registry
            ),
            "ACTIVE_WATCH_STREAMS": Gauge(
                "blindsided_active_watch_streams", "active", registry=self.registry
            ),
            "WATCH_STREAMS": Counter(
                "blindsided_watch_streams_total", "streams", ["outcome"],
                registry=self.registry,
            ),
            "WATCH_UPDATES": Counter(
                "blindsided_watch_updates_total", "updates", registry=self.registry
            ),
        }
        for name, collector in collectors.items():
            patcher = mock.patch.object(instrumentation, name, collector)
            patcher.start()
            self.addCleanup(patcher.stop)

    def value(self, name, labels=None):
        return self.registry.get_sample_value(name, labels or {})

    def test_storage_state_is_one_hot_and_repeatable(self):
        instrumentation.refresh_storage_state_metrics(
            role="unassigned", ready=False, epoch=0
        )
        instrumentation.refresh_storage_state_metrics(
            role="unassigned", ready=False, epoch=0
        )
        self.assertEqual(self.value("blindsided_storage_role", {"role": "unassigned"}), 1)
        self.assertEqual(self.value("blindsided_storage_role", {"role": "primary"}), 0)
        self.assertEqual(self.value("blindsided_storage_role", {"role": "backup"}), 0)
        self.assertEqual(self.value("blindsided_storage_ready"), 0)
        self.assertEqual(self.value("blindsided_storage_epoch"), 0)

    def test_promotion_updates_epoch_role_and_existing_readiness_barrier(self):
        judge = make_judge(role="backup")
        judge._storage_metrics_ready = False
        judge._refresh_storage_state_metrics_locked()

        begun = judge.BeginPrimaryPromotion(
            pb2.BeginPrimaryPromotionRequest(epoch=4), NoopContext()
        )
        self.assertTrue(begun.accepted)
        self.assertEqual(self.value("blindsided_storage_role", {"role": "primary"}), 1)
        self.assertEqual(self.value("blindsided_storage_ready"), 0)
        self.assertEqual(self.value("blindsided_storage_epoch"), 4)

        completed = judge.CompletePrimaryPromotion(
            pb2.CompletePrimaryPromotionRequest(epoch=4, backup_address="backup:1"),
            NoopContext(),
        )
        self.assertTrue(completed.success)
        self.assertEqual(self.value("blindsided_storage_ready"), 1)

    def test_stale_promotion_does_not_overwrite_newer_metrics(self):
        judge = make_judge(role="primary")
        judge.current_epoch = 9
        judge._storage_metrics_ready = True
        judge._refresh_storage_state_metrics_locked()

        response = judge.BeginPrimaryPromotion(
            pb2.BeginPrimaryPromotionRequest(epoch=8), NoopContext()
        )
        self.assertFalse(response.accepted)
        self.assertEqual(self.value("blindsided_storage_role", {"role": "primary"}), 1)
        self.assertEqual(self.value("blindsided_storage_ready"), 1)
        self.assertEqual(self.value("blindsided_storage_epoch"), 9)

    def test_backup_sync_readiness_is_guarded_by_role_and_epoch(self):
        judge = make_judge(role="backup")
        judge.current_epoch = 7
        judge._storage_metrics_ready = False
        judge._refresh_storage_state_metrics_locked()
        judge.synchronization_client = mock.Mock()
        judge.synchronization_client.fetch_full_state.return_value = pb2.StateResponse(ok=True)
        judge._report_synchronization_complete = mock.Mock(
            return_value=pb2.SynchronizationCompleteResponse(success=True)
        )
        judge._configure_primary_backup = mock.Mock(return_value=True)

        self.assertTrue(judge._synchronize_from_primary("primary:1", 7))
        self.assertEqual(self.value("blindsided_storage_role", {"role": "backup"}), 1)
        self.assertEqual(self.value("blindsided_storage_ready"), 1)
        self.assertEqual(self.value("blindsided_storage_epoch"), 7)

        judge.replica_role = "primary"
        judge.current_epoch = 8
        judge._storage_metrics_ready = False
        judge._refresh_storage_state_metrics_locked()
        judge._configure_primary_backup = mock.Mock(return_value=True)
        self.assertFalse(judge._synchronize_from_primary("primary:1", 7))
        self.assertEqual(self.value("blindsided_storage_role", {"role": "primary"}), 1)
        self.assertEqual(self.value("blindsided_storage_ready"), 0)
        self.assertEqual(self.value("blindsided_storage_epoch"), 8)

    def test_watch_completion_counts_update_and_preserves_object(self):
        response = pb2.GetStoredAuctionResponse(
            ok=True,
            auction=pb2.Auction(
                auction_id="auction-1", version=2, state=pb2.AUCTION_STATE_REVEALED
            ),
        )
        service = WatchService(response=response)
        stream = service.WatchAuction(pb2.AuctionRequest(auction_id="auction-1"), NoopContext())

        update = next(stream)
        self.assertEqual(update.version, 2)
        self.assertEqual(self.value("blindsided_active_watch_streams"), 1)
        with self.assertRaises(StopIteration):
            next(stream)
        self.assertEqual(self.value("blindsided_active_watch_streams"), 0)
        self.assertEqual(self.value("blindsided_watch_updates_total"), 1)
        self.assertEqual(self.value(
            "blindsided_watch_streams_total", {"outcome": "completed"}
        ), 1)

    def test_watch_cancellation_and_close_do_not_leak(self):
        cancelled = WatchService().WatchAuction(
            pb2.AuctionRequest(auction_id="auction-1"), InactiveContext()
        )
        with self.assertRaises(StopIteration):
            next(cancelled)
        self.assertEqual(self.value("blindsided_active_watch_streams"), 0)
        self.assertEqual(self.value(
            "blindsided_watch_streams_total", {"outcome": "cancelled"}
        ), 1)

        response = pb2.GetStoredAuctionResponse(
            ok=True,
            auction=pb2.Auction(
                auction_id="auction-1", version=1, state=pb2.AUCTION_STATE_OPEN
            ),
        )
        closed = WatchService(response=response).WatchAuction(
            pb2.AuctionRequest(auction_id="auction-1"), NoopContext()
        )
        next(closed)
        closed.close()
        self.assertEqual(self.value("blindsided_active_watch_streams"), 0)
        self.assertEqual(self.value(
            "blindsided_watch_streams_total", {"outcome": "cancelled"}
        ), 2)

    def test_watch_failure_records_once_without_leaking(self):
        error = RuntimeError("boom")
        stream = WatchService(error=error).WatchAuction(
            pb2.AuctionRequest(auction_id="auction-1"), NoopContext()
        )
        with self.assertRaisesRegex(RuntimeError, "boom"):
            next(stream)
        self.assertEqual(self.value("blindsided_active_watch_streams"), 0)
        self.assertEqual(self.value(
            "blindsided_watch_streams_total", {"outcome": "failure"}
        ), 1)
