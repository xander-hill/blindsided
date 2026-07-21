from unittest import mock

from google.protobuf import timestamp_pb2

from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import (
    BackendTestCase,
    NoopContext,
    active_bid,
    future_timestamp,
    make_judge,
)


class AuctionLifecycleSpecificationTests(BackendTestCase):
    """Contract tests for docs/auction-specification.md section 2.1."""

    def test_auction_begins_open(self):
        judge = make_judge(role="primary")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="lifecycle-open",
                    seller_id="seller-a",
                    title="Lifecycle Open",
                    reserve_price=100.0,
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(judge.auction_store["lifecycle-open"].state, pb2.AUCTION_STATE_OPEN)

    def test_system_performs_reveal_transition_from_open_to_revealed(self):
        judge = make_judge(role="primary")
        judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="lifecycle-reveal",
                    seller_id="seller-a",
                    title="Lifecycle Reveal",
                    reserve_price=100.0,
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="lifecycle-reveal",
                    seller_id="seller-a",
                    version=1,
                ),
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(
            judge.auction_store["lifecycle-reveal"].state,
            pb2.AUCTION_STATE_REVEALED,
        )
        self.assertEqual(judge.auction_store["lifecycle-reveal"].version, 2)

    def test_reveal_transition_happens_at_most_once(self):
        judge = make_judge(role="primary")
        judge.auction_store["lifecycle-once"] = pb2.Auction(
            auction_id="lifecycle-once",
            version=3,
            state=pb2.AUCTION_STATE_REVEALED,
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(auction_id="lifecycle-once", version=3),
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["lifecycle-once"].state,
            pb2.AUCTION_STATE_REVEALED,
        )
        self.assertEqual(judge.auction_store["lifecycle-once"].version, 3)

    def test_deadline_read_stays_open_until_reveal_is_committed(self):
        judge = make_judge(role="primary")
        judge.auction_store["lifecycle-auto-reveal"] = pb2.Auction(
            auction_id="lifecycle-auto-reveal",
            reserve_price=500.0,
            version=1,
            state=pb2.AUCTION_STATE_OPEN,
            next_bid_sequence=1,
            ends_at=timestamp_pb2.Timestamp(seconds=1000),
        )

        with mock.patch("blindsided.storage.service.time.time", return_value=999.1):
            first_bid = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                    auction=pb2.Auction(
                        auction_id="lifecycle-auto-reveal",
                        bids={"buyer-a": active_bid(700.0)},
                    ),
                    expected_version=1,
                ),
                NoopContext(),
            )
        with mock.patch("blindsided.storage.service.time.time", return_value=999.2):
            withdrawal = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID,
                    auction=pb2.Auction(auction_id="lifecycle-auto-reveal"),
                    bidder_id="buyer-a",
                    expected_version=2,
                ),
                NoopContext(),
            )
        with mock.patch("blindsided.storage.service.time.time", return_value=999.3):
            final_bid = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
                    auction=pb2.Auction(
                        auction_id="lifecycle-auto-reveal",
                        bids={"buyer-b": active_bid(800.0)},
                    ),
                    expected_version=3,
                ),
                NoopContext(),
            )
        with mock.patch("blindsided.storage.service.time.time", return_value=1000.0):
            response = judge.GetAuction(
                pb2.StorageGetAuctionRequest(auction_id="lifecycle-auto-reveal", epoch=judge.current_epoch),
                NoopContext(),
            )
            reveal = judge.ApplyAuctionMutation(
                pb2.AuctionMutationRequest(
                    mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
                    auction=pb2.Auction(
                        auction_id="lifecycle-auto-reveal",
                        version=4,
                    ),
                ),
                NoopContext(),
            )

        self.assertTrue(first_bid.success)
        self.assertTrue(withdrawal.success)
        self.assertTrue(final_bid.success)
        self.assertTrue(response.ok)
        self.assertEqual(response.auction.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(response.auction.version, 4)
        self.assertFalse(response.auction.HasField("result"))
        self.assertTrue(reveal.success)
        committed = judge.auction_store["lifecycle-auto-reveal"]
        self.assertEqual(committed.state, pb2.AUCTION_STATE_REVEALED)
        self.assertEqual(committed.version, 5)
        self.assertNotIn("buyer-a", committed.bids)
        self.assertEqual(committed.bids["buyer-b"], active_bid(800.0, 2))
        self.assertEqual(
            committed.result.outcome,
            pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE,
        )
        self.assertEqual(committed.result.winning_bidder_id, "buyer-b")
        self.assertEqual(committed.result.winning_amount, 800.0)

    def test_revealed_is_terminal_and_rejects_later_bid_mutations(self):
        judge = make_judge(role="primary")
        judge.auction_store["lifecycle-terminal"] = pb2.Auction(
            auction_id="lifecycle-terminal",
            version=4,
            state=pb2.AUCTION_STATE_REVEALED,
            bids={"buyer-a": active_bid(500.0, 1)},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="lifecycle-terminal",
                    version=4,
                    bids={"buyer-b": active_bid(750.0)},
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("buyer-b", judge.auction_store["lifecycle-terminal"].bids)
        self.assertEqual(judge.auction_store["lifecycle-terminal"].version, 4)

    def test_revealed_auction_cannot_transition_back_to_open(self):
        judge = make_judge(role="primary")
        judge.auction_store["lifecycle-no-reopen"] = pb2.Auction(
            auction_id="lifecycle-no-reopen",
            version=2,
            state=pb2.AUCTION_STATE_REVEALED,
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="lifecycle-no-reopen",
                    version=2,
                    state=pb2.AUCTION_STATE_OPEN,
                ),
                mutation_type=pb2.AUCTION_MUTATION_TYPE_PLACE_BID,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["lifecycle-no-reopen"].state,
            pb2.AUCTION_STATE_REVEALED,
        )
        self.assertEqual(judge.auction_store["lifecycle-no-reopen"].version, 2)

    def test_reveal_requires_an_existing_open_auction(self):
        judge = make_judge(role="primary")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(auction_id="missing-auction"),
                mutation_type=pb2.AUCTION_MUTATION_TYPE_REVEAL,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("missing-auction", judge.auction_store)
