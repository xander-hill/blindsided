from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, NoopContext, make_judge


class AuctionLifecycleNegativeSpecificationTests(BackendTestCase):
    """Negative contract tests for docs/auction-specification.md section 2.1."""

    def test_creation_request_cannot_start_auction_revealed(self):
        judge = make_judge(role="backup")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="negative-start-revealed",
                    title="Invalid Revealed Auction",
                    state=pb2.AUCTION_STATE_REVEALED,
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("negative-start-revealed", judge.auction_store)

    def test_non_reveal_mutation_cannot_transition_open_auction_to_revealed(self):
        judge = make_judge(role="backup")
        judge.auction_store["negative-direct-reveal"] = pb2.Auction(
            auction_id="negative-direct-reveal",
            title="Direct Reveal",
            version=5,
            state=pb2.AUCTION_STATE_OPEN,
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="negative-direct-reveal",
                    version=5,
                    state=pb2.AUCTION_STATE_REVEALED,
                ),
                is_reveal_event=False,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["negative-direct-reveal"].state,
            pb2.AUCTION_STATE_OPEN,
        )
        self.assertEqual(judge.auction_store["negative-direct-reveal"].version, 5)

    def test_forced_consistency_skip_cannot_directly_reveal_open_auction(self):
        judge = make_judge(role="backup")
        judge.auction_store["negative-forced-direct-reveal"] = pb2.Auction(
            auction_id="negative-forced-direct-reveal",
            title="Forced Direct Reveal",
            version=2,
            state=pb2.AUCTION_STATE_OPEN,
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="negative-forced-direct-reveal",
                    version=2,
                    state=pb2.AUCTION_STATE_REVEALED,
                ),
                skip_consistency_check=True,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["negative-forced-direct-reveal"].state,
            pb2.AUCTION_STATE_OPEN,
        )
        self.assertEqual(judge.auction_store["negative-forced-direct-reveal"].version, 2)

    def test_reveal_event_with_stale_version_does_not_reveal(self):
        judge = make_judge(role="backup")
        judge.auction_store["negative-stale-reveal"] = pb2.Auction(
            auction_id="negative-stale-reveal",
            title="Stale Reveal",
            version=8,
            state=pb2.AUCTION_STATE_OPEN,
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="negative-stale-reveal",
                    version=7,
                ),
                is_reveal_event=True,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["negative-stale-reveal"].state,
            pb2.AUCTION_STATE_OPEN,
        )
        self.assertEqual(judge.auction_store["negative-stale-reveal"].version, 8)

    def test_second_reveal_event_does_not_advance_terminal_auction(self):
        judge = make_judge(role="backup")
        judge.auction_store["negative-second-reveal"] = pb2.Auction(
            auction_id="negative-second-reveal",
            title="Second Reveal",
            version=3,
            state=pb2.AUCTION_STATE_REVEALED,
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="negative-second-reveal",
                    version=3,
                ),
                is_reveal_event=True,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["negative-second-reveal"].state,
            pb2.AUCTION_STATE_REVEALED,
        )
        self.assertEqual(judge.auction_store["negative-second-reveal"].version, 3)

    def test_forced_consistency_skip_cannot_mutate_revealed_auction(self):
        judge = make_judge(role="backup")
        judge.auction_store["negative-forced-terminal"] = pb2.Auction(
            auction_id="negative-forced-terminal",
            title="Forced Terminal",
            version=9,
            state=pb2.AUCTION_STATE_REVEALED,
            bids={"buyer-a": 900.0},
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="negative-forced-terminal",
                    version=9,
                    bids={"buyer-b": 1000.0},
                ),
                skip_consistency_check=True,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("buyer-b", judge.auction_store["negative-forced-terminal"].bids)
        self.assertEqual(judge.auction_store["negative-forced-terminal"].version, 9)
