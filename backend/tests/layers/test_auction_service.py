from blindsided.auction_service.service import BlindSidedService
from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, ChannelContext, NoopContext


class FakeJudgeStub:
    def __init__(self):
        self.commits: list[pb2.CommitRequest] = []
        self.queries: list[pb2.QueryRequest] = []
        self.commit_responses: list[pb2.CommitResponse] = []
        self.query_responses: list[pb2.QueryResponse] = []

    def CommitToVault(self, request, timeout=None):
        self.commits.append(request)
        if self.commit_responses:
            return self.commit_responses.pop(0)
        return pb2.CommitResponse(success=True, current_version=1, message="ok")

    def QueryVault(self, request, timeout=None):
        self.queries.append(request)
        if self.query_responses:
            return self.query_responses.pop(0)
        return pb2.QueryResponse(ok=True)


class TestableBlindSidedService(BlindSidedService):
    def __init__(self, stub: FakeJudgeStub, primary: str | None = "judge:50051"):
        self.stub = stub
        self.primary = primary

    def _get_primary_address(self, force_refresh=False):
        return self.primary

    def _get_all_judge_addresses(self):
        return [self.primary] if self.primary else []

    def _judge_stub(self, address: str):
        return self.stub, ChannelContext()


class AuctionServiceTests(BackendTestCase):
    def test_open_auction_commits_to_primary_vault(self):
        stub = FakeJudgeStub()
        service = TestableBlindSidedService(stub)

        response = service.OpenAuction(
            pb2.OpenRequest(auction=pb2.Auction(auction_id="auction-1")),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.auction_id, "auction-1")
        self.assertEqual(stub.commits[0].auction.auction_id, "auction-1")
        self.assertFalse(stub.commits[0].is_reveal_event)

    def test_status_masks_bids_before_reveal(self):
        stub = FakeJudgeStub()
        stub.query_responses.append(pb2.QueryResponse(
            ok=True,
            auctions=[
                pb2.Auction(
                    auction_id="auction-1",
                    bids={"buyer-a": 100.0},
                    state=pb2.AUCTION_STATE_OPEN,
                )
            ],
        ))
        service = TestableBlindSidedService(stub)

        response = service.GetStatus(
            pb2.StatusRequest(auction_id="auction-1"),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.auction.auction_id, "auction-1")
        self.assertEqual(dict(response.auction.bids), {})

    def test_status_reveals_bids_after_gavel(self):
        stub = FakeJudgeStub()
        stub.query_responses.append(pb2.QueryResponse(
            ok=True,
            auctions=[
                pb2.Auction(
                    auction_id="auction-1",
                    bids={"buyer-a": 100.0},
                    state=pb2.AUCTION_STATE_REVEALED,
                )
            ],
        ))
        service = TestableBlindSidedService(stub)

        response = service.GetStatus(
            pb2.StatusRequest(auction_id="auction-1"),
            NoopContext(),
        )

        self.assertTrue(response.ok)
        self.assertEqual(response.auction.bids["buyer-a"], 100.0)

    def test_bid_retries_with_latest_version_after_stale_conflict(self):
        stub = FakeJudgeStub()
        stub.commit_responses.extend([
            pb2.CommitResponse(success=False, message="Fog conflict: Stale version."),
            pb2.CommitResponse(success=True, current_version=8, message="ok"),
        ])
        stub.query_responses.append(pb2.QueryResponse(
            ok=True,
            auctions=[pb2.Auction(auction_id="auction-1", version=7)],
        ))
        service = TestableBlindSidedService(stub)

        response = service.PlaceSecretBid(
            pb2.BidRequest(
                auction_id="auction-1",
                buyer_id="buyer-a",
                amount=250.0,
                expected_version=6,
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(stub.commits[0].auction.version, 6)
        self.assertEqual(stub.commits[1].auction.version, 7)

    def test_drop_gavel_returns_public_gavel_response(self):
        stub = FakeJudgeStub()
        stub.query_responses.append(pb2.QueryResponse(
            ok=True,
            auctions=[pb2.Auction(auction_id="auction-1", version=3)],
        ))
        stub.commit_responses.append(pb2.CommitResponse(
            success=True,
            current_version=4,
            message="Vault updated.",
        ))
        service = TestableBlindSidedService(stub)

        response = service.DropTheGavel(
            pb2.GavelRequest(auction_id="auction-1"),
            NoopContext(),
        )

        self.assertIsInstance(response, pb2.GavelResponse)
        self.assertTrue(response.ok)
        self.assertEqual(response.final_version, 4)
        self.assertTrue(stub.commits[0].is_reveal_event)

    def test_opaque_update_uses_public_auction_update_fields(self):
        service = TestableBlindSidedService(FakeJudgeStub())

        hidden = service._mask_for_opaque_fog(pb2.Auction(
            bids={"a": 100.0, "b": 250.0},
            reserve_met=True,
        ))
        revealed = service._mask_for_opaque_fog(pb2.Auction(
            bids={"a": 100.0, "b": 250.0},
            state=pb2.AUCTION_STATE_REVEALED,
        ))

        self.assertEqual(hidden.low_range, 100.0)
        self.assertEqual(hidden.high_range, 250.0)
        self.assertEqual(hidden.bidder_count, 2)
        self.assertTrue(hidden.reserve_status)
        self.assertEqual(revealed.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(revealed.final_price, 250.0)
        self.assertEqual(revealed.winner_id, "b")
