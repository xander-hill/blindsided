from unittest import mock
from uuid import UUID

import grpc

from blindsided.auction_service.service import AuctionService, PrimaryAssignment
from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import (
    BackendTestCase,
    ChannelContext,
    NoopContext,
    active_bid,
    future_timestamp,
)


class FakeJudgeStub:
    def __init__(self):
        self.mutations: list[pb2.AuctionMutationRequest] = []
        self.gets: list[pb2.GetAuctionRequest] = []
        self.searches: list[pb2.SearchAuctionsRequest] = []
        self.mutation_responses: list[pb2.AuctionMutationResponse] = []
        self.mutation_errors: list[grpc.RpcError] = []
        self.get_responses: list[pb2.GetStoredAuctionResponse] = []
        self.search_responses: list[pb2.GetStoredAuctionsResponse] = []

    def ApplyAuctionMutation(self, request, timeout=None):
        self.mutations.append(request)
        if self.mutation_errors:
            raise self.mutation_errors.pop(0)
        if self.mutation_responses:
            return self.mutation_responses.pop(0)
        return pb2.AuctionMutationResponse(success=True, current_version=1, message="ok")

    def GetAuction(self, request, timeout=None):
        self.gets.append(request)
        if self.get_responses:
            return self.get_responses.pop(0)
        return pb2.GetStoredAuctionResponse(ok=False)

    def SearchAuctions(self, request, timeout=None):
        self.searches.append(request)
        if self.search_responses:
            return self.search_responses.pop(0)
        return pb2.GetStoredAuctionsResponse(ok=True)


class TestableAuctionService(AuctionService):
    def __init__(
        self,
        stub: FakeJudgeStub,
        primary_address: str | None = "judge:50051",
        primary_epoch: int = 7,
    ):
        self.stub = stub
        self.primary_address = primary_address
        self.primary_epoch = primary_epoch
        self.storage_addresses: list[str] = []

    def _get_primary_assignment(self, timeout_seconds=2.0):
        if not self.primary_address:
            return None
        return PrimaryAssignment(self.primary_address, self.primary_epoch)

    def _get_storage_node_addresses(self):
        return [self.primary_address] if self.primary_address else []

    def _create_storage_stub(self, address: str):
        self.storage_addresses.append(address)
        return self.stub, ChannelContext()


class FailoverRoutingAuctionService(AuctionService):
    def __init__(self, assignments, stubs):
        self.assignments = list(assignments)
        self.stubs = stubs
        self.events = []

    def _get_primary_assignment(self, timeout_seconds=2.0):
        assignment = self.assignments.pop(0)
        self.events.append(
            f"controller:{assignment.address}" if assignment else "controller:none"
        )
        return assignment

    def _create_storage_stub(self, address):
        self.events.append(f"mutation:{address}")
        return self.stubs[address], ChannelContext()

    def _failover_recovery_window(self):
        return 5


class FakeRpcError(grpc.RpcError):
    def details(self):
        return "temporary transport failure"

    def code(self):
        return grpc.StatusCode.UNKNOWN


class StatusRpcError(FakeRpcError):
    def __init__(self, status_code):
        self.status_code = status_code

    def code(self):
        return self.status_code


class ExpiredContext(NoopContext):
    def time_remaining(self):
        return 0


class AuctionServiceTests(BackendTestCase):
    def _public_field_names(self, message):
        return {field.name for field in message.DESCRIPTOR.fields}

    def _assert_no_bid_data_exposed(self, message):
        field_names = self._public_field_names(message)
        self.assertNotIn("bids", field_names)
        self.assertNotIn("reserve_price", field_names)
        rendered = str(message)
        self.assertNotIn("losing-bidder", rendered)
        self.assertNotIn("hidden-bidder", rendered)
        self.assertNotIn("12345.5", rendered)
        self.assertNotIn("67890", rendered)

    def _run_interrupted_mutation(self, operation, status_code):
        primary_a = FakeJudgeStub()
        primary_a.mutation_errors.append(StatusRpcError(status_code))
        primary_b = FakeJudgeStub()
        primary_b.mutation_responses.append(
            pb2.AuctionMutationResponse(
                success=True,
                current_version=7,
                auction_id="generated-auction-id",
            )
        )
        service = FailoverRoutingAuctionService(
            assignments=[
                PrimaryAssignment("primary-a:50051", 4),
                None,
                PrimaryAssignment("primary-b:50051", 5),
            ],
            stubs={
                "primary-a:50051": primary_a,
                "primary-b:50051": primary_b,
            },
        )
        requests = {
            "create": pb2.CreateAuctionRequest(
                seller_id="seller-a",
                title="Interrupted creation",
                reserve_price=100,
                request_id="interrupted-create",
            ),
            "bid": pb2.BidRequest(
                auction_id="auction-1",
                bidder_id="buyer-a",
                amount=250,
                expected_version=6,
                request_id="interrupted-bid",
            ),
            "withdrawal": pb2.WithdrawBidRequest(
                auction_id="auction-1",
                bidder_id="buyer-a",
                expected_version=6,
                request_id="interrupted-withdrawal",
            ),
            "reveal": pb2.RevealAuctionRequest(
                auction_id="auction-1",
                seller_id="seller-a",
                expected_version=6,
                request_id="interrupted-reveal",
            ),
        }
        methods = {
            "create": service.CreateAuction,
            "bid": service.PlaceBid,
            "withdrawal": service.WithdrawBid,
            "reveal": service.RevealAuction,
        }
        with (
            mock.patch(
                "blindsided.auction_service.service.uuid4",
                return_value="generated-auction-id",
            ),
            mock.patch(
                "blindsided.auction_service.service.random.uniform",
                return_value=0.25,
            ),
            mock.patch("blindsided.auction_service.service.time.sleep"),
        ):
            response = methods[operation](requests[operation], NoopContext())
        return response, primary_a.mutations[0], primary_b.mutations[0], service.events

    def test_interrupted_mutation_waits_for_ready_primary(self):
        for operation in ("create", "bid", "withdrawal", "reveal"):
            with self.subTest(operation=operation):
                response, _, _, events = self._run_interrupted_mutation(
                    operation,
                    grpc.StatusCode.UNAVAILABLE,
                )
                self.assertEqual(
                    events,
                    [
                        "controller:primary-a:50051",
                        "mutation:primary-a:50051",
                        "controller:none",
                        "controller:primary-b:50051",
                        "mutation:primary-b:50051",
                    ],
                )
                self.assertTrue(response.ok if operation in ("create", "reveal") else response.success)

    def test_interrupted_mutation_preserves_request_identity(self):
        for operation in ("create", "bid", "withdrawal", "reveal"):
            with self.subTest(operation=operation):
                _, first, second, _ = self._run_interrupted_mutation(
                    operation,
                    grpc.StatusCode.UNAVAILABLE,
                )
                self.assertEqual(first.request_id, second.request_id)
                self.assertEqual(first.epoch, 4)
                self.assertEqual(second.epoch, 5)
                first_logical = pb2.AuctionMutationRequest()
                first_logical.CopyFrom(first)
                second_logical = pb2.AuctionMutationRequest()
                second_logical.CopyFrom(second)
                first_logical.epoch = 0
                second_logical.epoch = 0
                self.assertEqual(first_logical, second_logical)
                if operation == "create":
                    self.assertEqual(first.auction.auction_id, "generated-auction-id")
                    self.assertEqual(second.auction.auction_id, "generated-auction-id")

    def test_deadline_exceeded_mutation_is_retried(self):
        for operation in ("create", "bid", "withdrawal", "reveal"):
            with self.subTest(operation=operation):
                response, first, second, _ = self._run_interrupted_mutation(
                    operation,
                    grpc.StatusCode.DEADLINE_EXCEEDED,
                )
                self.assertEqual(first.request_id, second.request_id)
                self.assertTrue(response.ok if operation in ("create", "reveal") else response.success)

    def test_non_transient_rpc_error_is_not_retried(self):
        for operation in ("create", "bid", "withdrawal", "reveal"):
            with self.subTest(operation=operation):
                stub = FakeJudgeStub()
                stub.mutation_errors.append(
                    StatusRpcError(grpc.StatusCode.INVALID_ARGUMENT)
                )
                service = TestableAuctionService(stub)
                requests = {
                    "create": pb2.CreateAuctionRequest(request_id="invalid-create"),
                    "bid": pb2.BidRequest(request_id="invalid-bid"),
                    "withdrawal": pb2.WithdrawBidRequest(request_id="invalid-withdrawal"),
                    "reveal": pb2.RevealAuctionRequest(request_id="invalid-reveal"),
                }
                methods = {
                    "create": service.CreateAuction,
                    "bid": service.PlaceBid,
                    "withdrawal": service.WithdrawBid,
                    "reveal": service.RevealAuction,
                }
                with mock.patch.object(service, "_wait_for_ready_primary") as wait:
                    methods[operation](requests[operation], NoopContext())
                self.assertEqual(len(stub.mutations), 1)
                wait.assert_not_called()

    def test_unresolved_mutation_returns_unknown_outcome(self):
        for operation in ("create", "bid", "withdrawal", "reveal"):
            with self.subTest(operation=operation):
                stub = FakeJudgeStub()
                stub.mutation_errors.append(
                    StatusRpcError(grpc.StatusCode.UNAVAILABLE)
                )
                service = TestableAuctionService(stub)
                requests = {
                    "create": pb2.CreateAuctionRequest(request_id="unknown-create"),
                    "bid": pb2.BidRequest(request_id="unknown-bid"),
                    "withdrawal": pb2.WithdrawBidRequest(request_id="unknown-withdrawal"),
                    "reveal": pb2.RevealAuctionRequest(request_id="unknown-reveal"),
                }
                methods = {
                    "create": service.CreateAuction,
                    "bid": service.PlaceBid,
                    "withdrawal": service.WithdrawBid,
                    "reveal": service.RevealAuction,
                }
                with mock.patch.object(
                    service,
                    "_wait_for_ready_primary",
                    return_value=None,
                ):
                    response = methods[operation](requests[operation], NoopContext())
                self.assertTrue(response.retryable)
                self.assertTrue(response.outcome_unknown)
                self.assertEqual(response.request_id, requests[operation].request_id)
                self.assertIn("UNAVAILABLE", response.message)
                self.assertIn("same request_id", response.message)

    def test_mutation_requires_request_id(self):
        for operation in ("create", "bid", "withdrawal", "reveal"):
            for request_id in ("", "   "):
                with self.subTest(operation=operation, request_id=request_id):
                    service = TestableAuctionService(FakeJudgeStub())
                    requests = {
                        "create": pb2.CreateAuctionRequest(request_id=request_id),
                        "bid": pb2.BidRequest(request_id=request_id),
                        "withdrawal": pb2.WithdrawBidRequest(request_id=request_id),
                        "reveal": pb2.RevealAuctionRequest(request_id=request_id),
                    }
                    methods = {
                        "create": service.CreateAuction,
                        "bid": service.PlaceBid,
                        "withdrawal": service.WithdrawBid,
                        "reveal": service.RevealAuction,
                    }
                    with mock.patch.object(
                        service,
                        "_get_primary_assignment",
                    ) as controller:
                        response = methods[operation](requests[operation], NoopContext())
                    controller.assert_not_called()
                    self.assertEqual(service.storage_addresses, [])
                    self.assertIn("request_id is required", response.message)

    def test_open_auction_mutations_to_primary_vault(self):
        stub = FakeJudgeStub()
        service = TestableAuctionService(stub)

        with mock.patch(
            "blindsided.auction_service.service.uuid4",
            return_value="generated-auction-id",
        ):
            response = service.CreateAuction(
                pb2.CreateAuctionRequest(
                    seller_id="seller-a",
                    title="Watch",
                    category="collectibles",
                    description="A clean example",
                    reserve_price=100.0,
                    ends_at=future_timestamp(),
                    request_id="open-auction-request",
                ),
                NoopContext(),
            )

        self.assertTrue(response.ok)
        self.assertEqual(response.auction_id, "generated-auction-id")
        self.assertEqual(stub.mutations[0].auction.auction_id, "generated-auction-id")
        self.assertEqual(stub.mutations[0].auction.seller_id, "seller-a")
        self.assertEqual(stub.mutations[0].auction.title, "Watch")
        self.assertEqual(stub.mutations[0].auction.category, "collectibles")
        self.assertEqual(stub.mutations[0].auction.description, "A clean example")
        self.assertEqual(stub.mutations[0].auction.reserve_price, 100.0)
        self.assertEqual(stub.mutations[0].auction.ends_at, future_timestamp())
        self.assertEqual(stub.mutations[0].auction.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(dict(stub.mutations[0].auction.bids), {})
        self.assertEqual(stub.mutations[0].auction.version, 0)
        self.assertEqual(stub.mutations[0].epoch, 7)
        self.assertEqual(
            stub.mutations[0].mutation_type,
            pb2.AUCTION_MUTATION_TYPE_CREATE,
        )

    def test_create_auction_generates_unique_uuid_ids(self):
        stub = FakeJudgeStub()
        service = TestableAuctionService(stub)

        first = service.CreateAuction(
            pb2.CreateAuctionRequest(
                seller_id="seller-a",
                title="First",
                reserve_price=100.0,
                ends_at=future_timestamp(),
                request_id="first-create-request",
            ),
            NoopContext(),
        )
        second = service.CreateAuction(
            pb2.CreateAuctionRequest(
                seller_id="seller-a",
                title="Second",
                reserve_price=100.0,
                ends_at=future_timestamp(),
                request_id="second-create-request",
            ),
            NoopContext(),
        )

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertNotEqual(first.auction_id, second.auction_id)
        self.assertEqual(str(UUID(first.auction_id)), first.auction_id)
        self.assertEqual(str(UUID(second.auction_id)), second.auction_id)

    def test_create_auction_forwards_client_request_id_and_uses_replayed_auction_id(self):
        stub = FakeJudgeStub()
        stub.mutation_responses.extend([
            pb2.AuctionMutationResponse(
                success=True,
                current_version=1,
                auction_id="original-auction-id",
                message="Vault updated.",
            ),
            pb2.AuctionMutationResponse(
                success=True,
                current_version=1,
                auction_id="original-auction-id",
                replayed=True,
                message="Vault updated.",
            ),
        ])
        service = TestableAuctionService(stub)

        first = service.CreateAuction(
            pb2.CreateAuctionRequest(
                seller_id="seller-a",
                title="Watch",
                reserve_price=100.0,
                ends_at=future_timestamp(),
                request_id="client-create-request",
            ),
            NoopContext(),
        )
        second = service.CreateAuction(
            pb2.CreateAuctionRequest(
                seller_id="seller-a",
                title="Watch",
                reserve_price=100.0,
                ends_at=future_timestamp(),
                request_id="client-create-request",
            ),
            NoopContext(),
        )

        self.assertTrue(first.ok)
        self.assertTrue(second.ok)
        self.assertEqual(first.auction_id, "original-auction-id")
        self.assertEqual(second.auction_id, "original-auction-id")
        self.assertEqual(stub.mutations[0].request_id, "client-create-request")
        self.assertEqual(stub.mutations[1].request_id, "client-create-request")

    def test_public_auction_projection_preserves_only_allowed_open_fields(self):
        stub = FakeJudgeStub()
        ends_at = future_timestamp()
        stub.get_responses.append(pb2.GetStoredAuctionResponse(
            ok=True,
            auction=pb2.Auction(
                auction_id="auction-1",
                seller_id="seller-a",
                title="Auction Metadata",
                category="collectibles",
                description="Metadata stays visible",
                bids={
                    "hidden-bidder-a": active_bid(12345.5, 1),
                    "hidden-bidder-b": active_bid(67890.0, 2),
                },
                reserve_price=500.0,
                state=pb2.AUCTION_STATE_OPEN,
                version=7,
                ends_at=ends_at,
            ),
        ))
        service = TestableAuctionService(stub)

        response = service.GetAuction(
            pb2.GetAuctionRequest(auction_id="auction-1"),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        public_auction = response.auction
        self.assertEqual(public_auction.auction_id, "auction-1")
        self.assertEqual(public_auction.seller_id, "seller-a")
        self.assertEqual(public_auction.title, "Auction Metadata")
        self.assertEqual(public_auction.category, "collectibles")
        self.assertEqual(public_auction.description, "Metadata stays visible")
        self.assertEqual(public_auction.ends_at, ends_at)
        self.assertEqual(public_auction.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(public_auction.bidder_count, 2)

        field_names = {field.name for field in public_auction.DESCRIPTOR.fields}
        self.assertNotIn("bids", field_names)
        self.assertNotIn("reserve_price", field_names)
        self.assertNotIn("reserve_met", field_names)
        self.assertNotIn("winning_amount", field_names)
        self.assertNotIn("winning_bidder_id", field_names)
        self.assertNotIn("has_winner", field_names)
        self.assertNotIn("hidden-bidder-a", str(public_auction))
        self.assertNotIn("hidden-bidder-b", str(public_auction))
        self.assertNotIn("12345.5", str(public_auction))
        self.assertNotIn("67890", str(public_auction))

    def test_get_auction_returns_only_requesting_bidder_active_bid(self):
        stub = FakeJudgeStub()
        stub.get_responses.append(pb2.GetStoredAuctionResponse(
            ok=True,
            auction=pb2.Auction(
                auction_id="auction-1",
                state=pb2.AUCTION_STATE_OPEN,
                bids={
                    "buyer-a": active_bid(250.0, 1),
                    "buyer-b": active_bid(900.0, 2),
                },
            ),
        ))
        service = TestableAuctionService(stub)

        response = service.GetAuction(
            pb2.GetAuctionRequest(
                auction_id="auction-1",
                bidder_id="buyer-a",
            ),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        self.assertEqual(stub.gets[0].bidder_id, "buyer-a")
        self.assertTrue(response.HasField("own_active_bid_amount"))
        self.assertEqual(response.own_active_bid_amount, 250.0)
        self.assertNotIn("acceptance_order", str(response))
        self.assertNotIn("900", str(response))
        self.assertNotIn("buyer-b", str(response))

    def test_get_auction_omits_active_bid_when_requesting_bidder_has_none(self):
        stub = FakeJudgeStub()
        stub.get_responses.append(pb2.GetStoredAuctionResponse(
            ok=True,
            auction=pb2.Auction(
                auction_id="auction-1",
                state=pb2.AUCTION_STATE_OPEN,
                bids={"buyer-b": active_bid(900.0, 1)},
            ),
        ))
        service = TestableAuctionService(stub)

        response = service.GetAuction(
            pb2.GetAuctionRequest(
                auction_id="auction-1",
                bidder_id="buyer-a",
            ),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        self.assertFalse(response.HasField("own_active_bid_amount"))
        self.assertNotIn("900", str(response))

    def test_live_updates_read_primary_committed_state(self):
        stub = FakeJudgeStub()
        stub.get_responses.append(pb2.GetStoredAuctionResponse(
            ok=True,
            auction=pb2.Auction(
                auction_id="auction-1",
                state=pb2.AUCTION_STATE_REVEALED,
                version=4,
                result=pb2.AuctionResult(
                    outcome=pb2.AUCTION_OUTCOME_NO_BIDS,
                ),
            ),
        ))
        service = TestableAuctionService(
            stub,
            primary_address="current-primary:50051",
        )

        update = next(service.WatchAuction(
            pb2.AuctionRequest(auction_id="auction-1"),
            NoopContext(),
        ))

        self.assertEqual(service.storage_addresses, ["current-primary:50051"])
        self.assertEqual(stub.gets[0].auction_id, "auction-1")
        self.assertEqual(update.version, 4)
        self.assertEqual(update.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(update.result.outcome, pb2.AUCTION_OUTCOME_NO_BIDS)

    def test_revealed_no_bids_public_result_exposes_no_bids(self):
        service = TestableAuctionService(FakeJudgeStub())
        public_auction = service._to_public_auction(pb2.Auction(
            auction_id="auction-1",
            state=pb2.AUCTION_STATE_REVEALED,
            version=3,
            reserve_price=20000.0,
            result=pb2.AuctionResult(
                outcome=pb2.AUCTION_OUTCOME_NO_BIDS,
                reserve_met=False,
                has_winner=False,
            ),
        ))
        update = service._to_public_auction_update(pb2.Auction(
            auction_id="auction-1",
            state=pb2.AUCTION_STATE_REVEALED,
            version=3,
            reserve_price=20000.0,
            result=pb2.AuctionResult(
                outcome=pb2.AUCTION_OUTCOME_NO_BIDS,
                reserve_met=False,
                has_winner=False,
            ),
        ))

        self.assertEqual(public_auction.state, pb2.AUCTION_STATE_REVEALED)
        self.assertTrue(public_auction.HasField("result"))
        self.assertEqual(public_auction.result.outcome, pb2.AUCTION_OUTCOME_NO_BIDS)
        self.assertFalse(public_auction.result.reserve_met)
        self.assertFalse(public_auction.result.has_winner)
        self.assertFalse(public_auction.result.HasField("winning_bidder_id"))
        self.assertFalse(public_auction.result.HasField("winning_amount"))
        self.assertEqual(public_auction.bidder_count, 0)
        self._assert_no_bid_data_exposed(public_auction)

        self.assertEqual(update.state, pb2.AUCTION_STATE_REVEALED)
        self.assertTrue(update.HasField("result"))
        self.assertEqual(update.result.outcome, pb2.AUCTION_OUTCOME_NO_BIDS)
        self.assertEqual(update.bidder_count, 0)
        self._assert_no_bid_data_exposed(update)

    def test_public_mapper_does_not_build_missing_storage_result(self):
        service = TestableAuctionService(FakeJudgeStub())
        auction = pb2.Auction(
            auction_id="legacy-revealed-without-result",
            state=pb2.AUCTION_STATE_REVEALED,
            version=3,
            reserve_price=500.0,
            bids={"winning-bidder": active_bid(750.0, 1)},
        )

        public_auction = service._to_public_auction(auction)
        update = service._to_public_auction_update(auction)

        self.assertEqual(public_auction.state, pb2.AUCTION_STATE_REVEALED)
        self.assertFalse(public_auction.HasField("result"))
        self.assertFalse(update.HasField("result"))
        self._assert_no_bid_data_exposed(public_auction)
        self._assert_no_bid_data_exposed(update)

    def test_revealed_reserve_not_met_public_result_hides_losing_bid_data(self):
        service = TestableAuctionService(FakeJudgeStub())
        auction = pb2.Auction(
            auction_id="auction-1",
            state=pb2.AUCTION_STATE_REVEALED,
            version=3,
            reserve_price=20000.0,
            bids={
                "losing-bidder-a": active_bid(12345.5, 1),
                "losing-bidder-b": active_bid(250.0, 2),
            },
            result=pb2.AuctionResult(
                outcome=pb2.AUCTION_OUTCOME_RESERVE_NOT_MET,
                reserve_met=False,
                has_winner=False,
            ),
        )

        public_auction = service._to_public_auction(auction)
        update = service._to_public_auction_update(auction)

        self.assertEqual(public_auction.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(
            public_auction.result.outcome,
            pb2.AUCTION_OUTCOME_RESERVE_NOT_MET,
        )
        self.assertFalse(public_auction.result.reserve_met)
        self.assertFalse(public_auction.result.has_winner)
        self.assertFalse(public_auction.result.HasField("winning_bidder_id"))
        self.assertFalse(public_auction.result.HasField("winning_amount"))
        self.assertEqual(public_auction.bidder_count, 2)
        self._assert_no_bid_data_exposed(public_auction)

        self.assertEqual(update.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(update.result.outcome, pb2.AUCTION_OUTCOME_RESERVE_NOT_MET)
        self.assertEqual(update.bidder_count, 2)
        self._assert_no_bid_data_exposed(update)

    def test_revealed_successful_sale_public_result_exposes_only_winner(self):
        service = TestableAuctionService(FakeJudgeStub())
        auction = pb2.Auction(
            auction_id="auction-1",
            state=pb2.AUCTION_STATE_REVEALED,
            version=3,
            reserve_price=500.0,
            bids={
                "winning-bidder": active_bid(750.0, 2),
                "losing-bidder": active_bid(600.0, 1),
            },
            result=pb2.AuctionResult(
                outcome=pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE,
                reserve_met=True,
                has_winner=True,
                winning_bidder_id="winning-bidder",
                winning_amount=750.0,
            ),
        )

        public_auction = service._to_public_auction(auction)
        update = service._to_public_auction_update(auction)

        self.assertEqual(public_auction.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(
            public_auction.result.outcome,
            pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE,
        )
        self.assertTrue(public_auction.result.reserve_met)
        self.assertTrue(public_auction.result.has_winner)
        self.assertEqual(public_auction.result.winning_bidder_id, "winning-bidder")
        self.assertEqual(public_auction.result.winning_amount, 750.0)
        self.assertEqual(public_auction.bidder_count, 2)
        self._assert_no_bid_data_exposed(public_auction)

        self.assertEqual(update.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(update.result.outcome, pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE)
        self.assertEqual(update.result.winning_bidder_id, "winning-bidder")
        self.assertEqual(update.result.winning_amount, 750.0)
        self.assertEqual(update.bidder_count, 2)
        self._assert_no_bid_data_exposed(update)

    def test_search_results_apply_same_pre_reveal_visibility_restrictions(self):
        stub = FakeJudgeStub()
        ends_at = future_timestamp()
        stub.search_responses.append(pb2.GetStoredAuctionsResponse(
            ok=True,
            count=1,
            auctions=[
                pb2.Auction(
                    auction_id="auction-1",
                    seller_id="seller-a",
                    title="Auction Metadata",
                    category="collectibles",
                    description="Metadata stays visible",
                    bids={
                        "hidden-bidder-a": active_bid(12345.5, 1),
                        "hidden-bidder-b": active_bid(67890.0, 2),
                    },
                    reserve_price=500.0,
                    state=pb2.AUCTION_STATE_OPEN,
                    version=7,
                    ends_at=ends_at,
                )
            ],
        ))
        service = TestableAuctionService(stub)

        response = service.SearchAuctions(
            pb2.SearchAuctionsRequest(query="auction"),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.count, 1)
        self.assertEqual(len(response.auctions), 1)
        public_auction = response.auctions[0]
        self.assertEqual(public_auction.auction_id, "auction-1")
        self.assertEqual(public_auction.seller_id, "seller-a")
        self.assertEqual(public_auction.title, "Auction Metadata")
        self.assertEqual(public_auction.category, "collectibles")
        self.assertEqual(public_auction.description, "Metadata stays visible")
        self.assertEqual(public_auction.ends_at, ends_at)
        self.assertEqual(public_auction.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(public_auction.bidder_count, 2)

        field_names = {field.name for field in public_auction.DESCRIPTOR.fields}
        self.assertNotIn("bids", field_names)
        self.assertNotIn("reserve_price", field_names)
        self.assertNotIn("reserve_met", field_names)
        self.assertNotIn("winning_amount", field_names)
        self.assertNotIn("winning_bidder_id", field_names)
        self.assertNotIn("has_winner", field_names)
        self.assertNotIn("high_range", field_names)
        self.assertNotIn("low_range", field_names)
        self.assertNotIn("hidden-bidder-a", str(public_auction))
        self.assertNotIn("hidden-bidder-b", str(public_auction))
        self.assertNotIn("12345.5", str(public_auction))
        self.assertNotIn("67890", str(public_auction))

    def test_bid_retries_with_latest_version_after_stale_conflict(self):
        stub = FakeJudgeStub()
        stub.mutation_responses.extend([
            pb2.AuctionMutationResponse(
                success=False,
                current_version=7,
                failure_reason=pb2.MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT,
                message="Fog conflict: Stale version.",
            ),
            pb2.AuctionMutationResponse(success=True, current_version=8, message="ok"),
        ])
        service = TestableAuctionService(stub)

        response = service.PlaceBid(
            pb2.BidRequest(
                auction_id="auction-1",
                bidder_id="buyer-a",
                amount=250.0,
                expected_version=6,
                request_id="bid-version-retry",
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(
            stub.mutations[0].mutation_type,
            pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
        )
        self.assertEqual(stub.mutations[0].expected_version, 6)
        self.assertEqual(stub.mutations[0].auction.version, 6)
        self.assertEqual(stub.mutations[0].request_id, stub.mutations[1].request_id)
        self.assertTrue(stub.mutations[0].request_id)
        self.assertEqual(stub.mutations[0].auction.bids["buyer-a"].amount, 250.0)
        self.assertEqual(
            stub.mutations[0].auction.bids["buyer-a"].acceptance_order,
            0,
        )
        self.assertEqual(stub.mutations[1].auction.version, 7)
        self.assertEqual(stub.mutations[1].expected_version, 7)
        self.assertEqual(stub.mutations[0].epoch, 7)
        self.assertEqual(stub.mutations[1].epoch, 7)
        self.assertEqual(stub.gets, [])

    def test_withdraw_bid_retries_with_latest_version_after_stale_conflict(self):
        stub = FakeJudgeStub()
        stub.mutation_responses.extend([
            pb2.AuctionMutationResponse(
                success=False,
                current_version=8,
                failure_reason=pb2.MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT,
                message="Fog conflict: Stale version.",
            ),
            pb2.AuctionMutationResponse(success=True, current_version=9, message="ok"),
        ])
        service = TestableAuctionService(stub)

        response = service.WithdrawBid(
            pb2.WithdrawBidRequest(
                auction_id="auction-1",
                bidder_id="buyer-a",
                expected_version=7,
                request_id="withdraw-version-retry",
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(response.final_version, 9)
        self.assertEqual(
            stub.mutations[0].mutation_type,
            pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
        )
        self.assertEqual(stub.mutations[0].auction.auction_id, "auction-1")
        self.assertEqual(stub.mutations[0].bidder_id, "buyer-a")
        self.assertEqual(stub.mutations[0].expected_version, 7)
        self.assertEqual(stub.mutations[1].expected_version, 8)
        self.assertEqual(stub.mutations[0].epoch, 7)
        self.assertEqual(stub.mutations[1].epoch, 7)
        self.assertEqual(stub.gets, [])

    def test_drop_gavel_returns_public_gavel_response(self):
        stub = FakeJudgeStub()
        stub.mutation_responses.append(pb2.AuctionMutationResponse(
            success=True,
            current_version=4,
            message="Vault updated.",
        ))
        service = TestableAuctionService(stub)

        response = service.RevealAuction(
            pb2.RevealAuctionRequest(
                auction_id="auction-1",
                request_id="reveal-request",
            ),
            NoopContext(),
        )

        self.assertIsInstance(response, pb2.RevealAuctionResponse)
        self.assertTrue(response.ok)
        self.assertEqual(response.final_version, 4)
        self.assertEqual(
            stub.mutations[0].mutation_type,
            pb2.AUCTION_MUTATION_TYPE_REVEAL,
        )
        self.assertEqual(stub.gets, [])

    def test_reveal_retries_with_storage_current_version_after_stale_conflict(self):
        stub = FakeJudgeStub()
        stub.mutation_responses.extend([
            pb2.AuctionMutationResponse(
                success=False,
                current_version=3,
                failure_reason=pb2.MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT,
                message="Fog conflict: Stale version.",
            ),
            pb2.AuctionMutationResponse(
                success=True,
                current_version=4,
                message="Vault updated.",
            ),
        ])
        service = TestableAuctionService(stub)

        response = service.RevealAuction(
            pb2.RevealAuctionRequest(
                auction_id="auction-1",
                expected_version=2,
                request_id="reveal-version-retry",
            ),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.final_version, 4)
        self.assertEqual(stub.mutations[0].expected_version, 2)
        self.assertEqual(stub.mutations[1].expected_version, 3)
        self.assertEqual(stub.mutations[1].auction.version, 3)
        self.assertEqual(stub.mutations[0].epoch, 7)
        self.assertEqual(stub.mutations[1].epoch, 7)
        self.assertEqual(stub.gets, [])

    def test_bid_does_not_retry_ambiguous_rpc_errors(self):
        stub = FakeJudgeStub()
        stub.mutation_errors.append(FakeRpcError())
        service = TestableAuctionService(stub)

        with mock.patch.object(service, "_wait_for_ready_primary") as recovery:
            response = service.PlaceBid(
                pb2.BidRequest(
                    auction_id="auction-1",
                    bidder_id="buyer-a",
                    amount=250.0,
                    expected_version=6,
                    request_id="non-transient-bid",
                ),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertIn("Judge connection failed", response.message)
        self.assertEqual(len(stub.mutations), 1)
        recovery.assert_not_called()

    def test_wait_for_ready_primary_polls_with_failover_backoff(self):
        service = TestableAuctionService(FakeJudgeStub())
        ready = PrimaryAssignment("new-primary:50051", 8)

        with (
            mock.patch.object(
                service,
                "_get_primary_assignment",
                side_effect=[None, ready],
            ) as get_primary,
            mock.patch.object(service, "_failover_recovery_window", return_value=5),
            mock.patch(
                "blindsided.auction_service.service.random.uniform",
                return_value=0.375,
            ) as jitter,
            mock.patch("blindsided.auction_service.service.time.sleep") as sleep,
        ):
            assignment = service._wait_for_ready_primary(NoopContext())

        self.assertEqual(assignment, ready)
        self.assertEqual(get_primary.call_count, 2)
        jitter.assert_called_once_with(0.25, 0.5)
        sleep.assert_called_once_with(0.375)

    def test_wait_for_ready_primary_stops_for_cancelled_or_expired_client(self):
        service = TestableAuctionService(FakeJudgeStub())
        cancelled = mock.Mock()
        cancelled.is_active.return_value = False

        with mock.patch.object(service, "_get_primary_assignment") as get_primary:
            self.assertIsNone(service._wait_for_ready_primary(cancelled))
            self.assertIsNone(service._wait_for_ready_primary(ExpiredContext()))

        get_primary.assert_not_called()

    def test_wait_for_ready_primary_stops_at_bounded_recovery_window(self):
        service = TestableAuctionService(FakeJudgeStub())

        with (
            mock.patch.object(service, "_get_primary_assignment", return_value=None) as get_primary,
            mock.patch.object(service, "_failover_recovery_window", return_value=0.1),
            mock.patch(
                "blindsided.auction_service.service.time.monotonic",
                side_effect=[0.0, 0.0, 0.1],
            ),
            mock.patch(
                "blindsided.auction_service.service.random.uniform",
                return_value=0.5,
            ),
            mock.patch("blindsided.auction_service.service.time.sleep") as sleep,
        ):
            assignment = service._wait_for_ready_primary(NoopContext())

        self.assertIsNone(assignment)
        get_primary.assert_called_once_with(timeout_seconds=0.1)
        sleep.assert_called_once_with(0.1)

    def test_bid_recovers_from_ambiguous_failover_rpc_statuses(self):
        for status_code in (
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.DEADLINE_EXCEEDED,
        ):
            with self.subTest(status_code=status_code):
                stub = FakeJudgeStub()
                stub.mutation_errors.append(StatusRpcError(status_code))
                stub.mutation_responses.append(
                    pb2.AuctionMutationResponse(success=True, current_version=7)
                )
                service = TestableAuctionService(
                    stub,
                    primary_address="old-primary:50051",
                    primary_epoch=7,
                )

                with mock.patch.object(
                    service,
                    "_wait_for_ready_primary",
                    return_value=PrimaryAssignment("new-primary:50051", 8),
                ) as recovery:
                    response = service.PlaceBid(
                        pb2.BidRequest(
                            auction_id="auction-1",
                            bidder_id="buyer-a",
                            amount=250.0,
                            expected_version=6,
                            request_id="stable-bid-request",
                        ),
                        NoopContext(),
                    )

                self.assertTrue(response.success)
                recovery.assert_called_once()
                self.assertEqual(
                    service.storage_addresses,
                    ["old-primary:50051", "new-primary:50051"],
                )
                self.assertEqual(len(stub.mutations), 2)
                self.assertEqual(stub.mutations[0].request_id, "stable-bid-request")
                self.assertEqual(stub.mutations[1].request_id, "stable-bid-request")
                self.assertEqual(stub.mutations[0].expected_version, 6)
                self.assertEqual(stub.mutations[1].expected_version, 6)
                self.assertEqual(stub.mutations[0].epoch, 7)
                self.assertEqual(stub.mutations[1].epoch, 8)

    def test_place_bid_waits_through_no_ready_primary_before_failover_retry(self):
        for status_code in (
            grpc.StatusCode.UNAVAILABLE,
            grpc.StatusCode.DEADLINE_EXCEEDED,
        ):
            with self.subTest(status_code=status_code):
                primary_a = FakeJudgeStub()
                primary_a.mutation_errors.append(StatusRpcError(status_code))
                primary_b = FakeJudgeStub()
                primary_b.mutation_responses.append(
                    pb2.AuctionMutationResponse(success=True, current_version=12)
                )
                service = FailoverRoutingAuctionService(
                    assignments=[
                        PrimaryAssignment("primary-a:50051", 4),
                        None,
                        PrimaryAssignment("primary-b:50051", 5),
                    ],
                    stubs={
                        "primary-a:50051": primary_a,
                        "primary-b:50051": primary_b,
                    },
                )

                with (
                    mock.patch(
                        "blindsided.auction_service.service.random.uniform",
                        return_value=0.25,
                    ),
                    mock.patch("blindsided.auction_service.service.time.sleep"),
                ):
                    response = service.PlaceBid(
                        pb2.BidRequest(
                            auction_id="auction-1",
                            bidder_id="buyer-a",
                            amount=275.0,
                            expected_version=11,
                            request_id="stable-place-bid-id",
                        ),
                        NoopContext(),
                    )

                self.assertTrue(response.success)
                self.assertEqual(
                    service.events,
                    [
                        "controller:primary-a:50051",
                        "mutation:primary-a:50051",
                        "controller:none",
                        "controller:primary-b:50051",
                        "mutation:primary-b:50051",
                    ],
                )
                self.assertEqual(len(primary_a.mutations), 1)
                self.assertEqual(len(primary_b.mutations), 1)
                first = primary_a.mutations[0]
                second = primary_b.mutations[0]
                self.assertEqual(first.request_id, "stable-place-bid-id")
                self.assertEqual(second.request_id, first.request_id)
                self.assertEqual(first.auction.auction_id, "auction-1")
                self.assertEqual(second.auction.auction_id, first.auction.auction_id)
                self.assertEqual(first.bidder_id, "buyer-a")
                self.assertEqual(second.bidder_id, first.bidder_id)
                self.assertEqual(first.auction.bids["buyer-a"].amount, 275.0)
                self.assertEqual(
                    second.auction.bids["buyer-a"].amount,
                    first.auction.bids["buyer-a"].amount,
                )
                self.assertEqual(first.epoch, 4)
                self.assertEqual(second.epoch, 5)
                self.assertEqual(first.expected_version, 11)
                self.assertEqual(second.expected_version, 11)
                self.assertEqual(first.auction.version, 11)
                self.assertEqual(second.auction.version, 11)

    def test_create_recovers_from_ambiguous_rpc_failure(self):
        stub = FakeJudgeStub()
        stub.mutation_errors.append(StatusRpcError(grpc.StatusCode.UNAVAILABLE))
        stub.mutation_responses.append(
            pb2.AuctionMutationResponse(
                success=True,
                current_version=1,
                auction_id="generated-auction-id",
            )
        )
        service = TestableAuctionService(stub, primary_epoch=7)

        with (
            mock.patch(
                "blindsided.auction_service.service.uuid4",
                return_value="generated-auction-id",
            ),
            mock.patch.object(
                service,
                "_wait_for_ready_primary",
                return_value=PrimaryAssignment("new-primary:50051", 8),
            ),
        ):
            response = service.CreateAuction(
                pb2.CreateAuctionRequest(
                    seller_id="seller-a",
                    title="Recovered creation",
                    reserve_price=100,
                    ends_at=future_timestamp(),
                    request_id="stable-request-id",
                ),
                NoopContext(),
            )

        self.assertTrue(response.ok)
        self.assertEqual(len(stub.mutations), 2)
        self.assertEqual(stub.mutations[0].request_id, "stable-request-id")
        self.assertEqual(stub.mutations[1].request_id, "stable-request-id")
        self.assertEqual(stub.mutations[0].auction, stub.mutations[1].auction)
        self.assertEqual(stub.mutations[0].auction.auction_id, "generated-auction-id")
        self.assertEqual(stub.mutations[0].epoch, 7)
        self.assertEqual(stub.mutations[1].epoch, 8)

    def test_withdrawal_recovers_from_ambiguous_rpc_failure(self):
        stub = FakeJudgeStub()
        stub.mutation_errors.append(
            StatusRpcError(grpc.StatusCode.DEADLINE_EXCEEDED)
        )
        stub.mutation_responses.append(
            pb2.AuctionMutationResponse(success=True, current_version=7)
        )
        service = TestableAuctionService(stub, primary_epoch=7)

        with mock.patch.object(
            service,
            "_wait_for_ready_primary",
            return_value=PrimaryAssignment("new-primary:50051", 8),
        ):
            response = service.WithdrawBid(
                pb2.WithdrawBidRequest(
                    auction_id="auction-1",
                    bidder_id="buyer-a",
                    expected_version=6,
                    request_id="stable-withdrawal-id",
                ),
                NoopContext(),
            )

        self.assertTrue(response.success)
        self.assertEqual(len(stub.mutations), 2)
        self.assertEqual(stub.mutations[0].request_id, "stable-withdrawal-id")
        self.assertEqual(stub.mutations[1].request_id, "stable-withdrawal-id")
        self.assertEqual(stub.mutations[0].expected_version, 6)
        self.assertEqual(stub.mutations[1].expected_version, 6)
        self.assertEqual(stub.mutations[0].epoch, 7)
        self.assertEqual(stub.mutations[1].epoch, 8)

    def test_reveal_recovers_from_ambiguous_rpc_failure(self):
        stub = FakeJudgeStub()
        stub.mutation_errors.append(StatusRpcError(grpc.StatusCode.UNAVAILABLE))
        stub.mutation_responses.append(
            pb2.AuctionMutationResponse(success=True, current_version=7)
        )
        service = TestableAuctionService(stub, primary_epoch=7)

        with mock.patch.object(
            service,
            "_wait_for_ready_primary",
            return_value=PrimaryAssignment("new-primary:50051", 8),
        ):
            response = service.RevealAuction(
                pb2.RevealAuctionRequest(
                    auction_id="auction-1",
                    seller_id="seller-a",
                    expected_version=6,
                    request_id="stable-reveal-id",
                ),
                NoopContext(),
            )

        self.assertTrue(response.ok)
        self.assertEqual(len(stub.mutations), 2)
        self.assertEqual(stub.mutations[0].request_id, "stable-reveal-id")
        self.assertEqual(stub.mutations[1].request_id, "stable-reveal-id")
        self.assertEqual(stub.mutations[0].auction, stub.mutations[1].auction)
        self.assertEqual(stub.mutations[0].expected_version, 6)
        self.assertEqual(stub.mutations[1].expected_version, 6)
        self.assertEqual(stub.mutations[0].epoch, 7)
        self.assertEqual(stub.mutations[1].epoch, 8)

    def test_mutations_return_retryable_unknown_outcome_when_recovery_expires(self):
        cases = (
            (
                "create",
                lambda service: service.CreateAuction(
                    pb2.CreateAuctionRequest(
                        seller_id="seller-a",
                        title="Unknown creation",
                        reserve_price=100,
                        request_id="unknown-create",
                    ),
                    NoopContext(),
                ),
                "unknown-create",
            ),
            (
                "bid",
                lambda service: service.PlaceBid(
                    pb2.BidRequest(
                        auction_id="auction-1",
                        bidder_id="buyer-a",
                        amount=250,
                        expected_version=4,
                        request_id="unknown-bid",
                    ),
                    NoopContext(),
                ),
                "unknown-bid",
            ),
            (
                "withdrawal",
                lambda service: service.WithdrawBid(
                    pb2.WithdrawBidRequest(
                        auction_id="auction-1",
                        bidder_id="buyer-a",
                        expected_version=4,
                        request_id="unknown-withdrawal",
                    ),
                    NoopContext(),
                ),
                "unknown-withdrawal",
            ),
            (
                "reveal",
                lambda service: service.RevealAuction(
                    pb2.RevealAuctionRequest(
                        auction_id="auction-1",
                        seller_id="seller-a",
                        expected_version=4,
                        request_id="unknown-reveal",
                    ),
                    NoopContext(),
                ),
                "unknown-reveal",
            ),
        )

        for operation, invoke, request_id in cases:
            with self.subTest(operation=operation):
                stub = FakeJudgeStub()
                stub.mutation_errors.append(
                    StatusRpcError(grpc.StatusCode.UNAVAILABLE)
                )
                service = TestableAuctionService(stub)
                with mock.patch.object(
                    service,
                    "_wait_for_ready_primary",
                    return_value=None,
                ) as recovery:
                    response = invoke(service)

                recovery.assert_called_once()
                self.assertTrue(response.retryable)
                self.assertTrue(response.outcome_unknown)
                self.assertEqual(response.request_id, request_id)
                self.assertIn("same request_id", response.message)
                self.assertEqual(len(stub.mutations), 1)
                if operation in ("create", "reveal"):
                    self.assertFalse(response.ok)
                else:
                    self.assertFalse(response.success)

    def test_opaque_update_uses_public_auction_update_fields(self):
        service = TestableAuctionService(FakeJudgeStub())

        hidden = service._to_public_auction_update(pb2.Auction(
            bids={
                "hidden-bidder-a": active_bid(12345.5, 1),
                "hidden-bidder-b": active_bid(67890.0, 2),
            },
            state=pb2.AUCTION_STATE_OPEN,
            version=9,
        ))
        revealed = service._to_public_auction_update(pb2.Auction(
            bids={"a": active_bid(100.0, 1), "b": active_bid(250.0, 2)},
            reserve_price=200.0,
            state=pb2.AUCTION_STATE_REVEALED,
            result=pb2.AuctionResult(
                outcome=pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE,
                reserve_met=True,
                has_winner=True,
                winning_bidder_id="b",
                winning_amount=250.0,
            ),
        ))

        self.assertEqual(hidden.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(hidden.bidder_count, 2)
        self.assertEqual(hidden.version, 9)
        field_names = {field.name for field in hidden.DESCRIPTOR.fields}
        self.assertNotIn("bids", field_names)
        self.assertNotIn("bidder_id", field_names)
        self.assertNotIn("bid_amount", field_names)
        self.assertNotIn("reserve_price", field_names)
        self.assertNotIn("reserve_met", field_names)
        self.assertNotIn("winning_amount", field_names)
        self.assertNotIn("winning_bidder_id", field_names)
        self.assertNotIn("high_range", field_names)
        self.assertNotIn("low_range", field_names)
        self.assertNotIn("hidden-bidder-a", str(hidden))
        self.assertNotIn("hidden-bidder-b", str(hidden))
        self.assertNotIn("12345.5", str(hidden))
        self.assertNotIn("67890", str(hidden))
        self.assertEqual(revealed.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(revealed.bidder_count, 2)
