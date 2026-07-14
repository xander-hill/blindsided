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
        judge = make_judge(role="backup")

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
        judge = make_judge(role="backup")
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
                auction=pb2.Auction(auction_id="lifecycle-reveal", version=1),
                is_reveal_event=True,
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
        judge = make_judge(role="backup")
        judge.auction_store["lifecycle-once"] = pb2.Auction(
            auction_id="lifecycle-once",
            version=3,
            state=pb2.AUCTION_STATE_REVEALED,
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(auction_id="lifecycle-once", version=3),
                is_reveal_event=True,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["lifecycle-once"].state,
            pb2.AUCTION_STATE_REVEALED,
        )
        self.assertEqual(judge.auction_store["lifecycle-once"].version, 3)

    def test_revealed_is_terminal_and_rejects_later_bid_mutations(self):
        judge = make_judge(role="backup")
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
        judge = make_judge(role="backup")
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
                skip_consistency_check=True,
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
        judge = make_judge(role="backup")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(auction_id="missing-auction"),
                is_reveal_event=True,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("missing-auction", judge.auction_store)
