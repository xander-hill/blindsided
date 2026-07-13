from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, NoopContext, make_judge


class AuctionLifecycleSpecificationTests(BackendTestCase):
    """Contract tests for docs/auction-specification.md section 2.1."""

    def test_auction_begins_open(self):
        judge = make_judge(role="backup")

        response = judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(
                    auction_id="lifecycle-open",
                    title="Lifecycle Open",
                )
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(judge.vault["lifecycle-open"].state, pb2.AUCTION_STATE_OPEN)

    def test_system_performs_reveal_transition_from_open_to_revealed(self):
        judge = make_judge(role="backup")
        judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(auction_id="lifecycle-reveal")
            ),
            NoopContext(),
        )

        response = judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(auction_id="lifecycle-reveal", version=1),
                is_reveal_event=True,
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(
            judge.vault["lifecycle-reveal"].state,
            pb2.AUCTION_STATE_REVEALED,
        )
        self.assertEqual(judge.vault["lifecycle-reveal"].version, 2)

    def test_reveal_transition_happens_at_most_once(self):
        judge = make_judge(role="backup")
        judge.vault["lifecycle-once"] = pb2.Auction(
            auction_id="lifecycle-once",
            version=3,
            state=pb2.AUCTION_STATE_REVEALED,
        )

        response = judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(auction_id="lifecycle-once", version=3),
                is_reveal_event=True,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.vault["lifecycle-once"].state,
            pb2.AUCTION_STATE_REVEALED,
        )
        self.assertEqual(judge.vault["lifecycle-once"].version, 3)

    def test_revealed_is_terminal_and_rejects_later_bid_mutations(self):
        judge = make_judge(role="backup")
        judge.vault["lifecycle-terminal"] = pb2.Auction(
            auction_id="lifecycle-terminal",
            version=4,
            state=pb2.AUCTION_STATE_REVEALED,
            bids={"buyer-a": 500.0},
        )

        response = judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(
                    auction_id="lifecycle-terminal",
                    version=4,
                    bids={"buyer-b": 750.0},
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("buyer-b", judge.vault["lifecycle-terminal"].bids)
        self.assertEqual(judge.vault["lifecycle-terminal"].version, 4)

    def test_revealed_auction_cannot_transition_back_to_open(self):
        judge = make_judge(role="backup")
        judge.vault["lifecycle-no-reopen"] = pb2.Auction(
            auction_id="lifecycle-no-reopen",
            version=2,
            state=pb2.AUCTION_STATE_REVEALED,
        )

        response = judge.CommitToVault(
            pb2.CommitRequest(
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
            judge.vault["lifecycle-no-reopen"].state,
            pb2.AUCTION_STATE_REVEALED,
        )
        self.assertEqual(judge.vault["lifecycle-no-reopen"].version, 2)

    def test_reveal_requires_an_existing_open_auction(self):
        judge = make_judge(role="backup")

        response = judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(auction_id="missing-auction"),
                is_reveal_event=True,
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("missing-auction", judge.vault)
