from unittest import mock

from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase, NoopContext, make_judge


class StorageServiceTests(BackendTestCase):
    def test_initial_commit_assigns_version_and_reserve_met(self):
        judge = make_judge(role="backup")

        response = judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    title="Chronograph",
                    reserve_price=500.0,
                    bids={"buyer-a": 650.0},
                )
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(response.current_version, 1)
        self.assertEqual(judge.auction_store["auction-1"].version, 1)
        self.assertTrue(judge.auction_store["auction-1"].reserve_met)

    def test_commit_rejects_stale_versions_and_preserves_existing_state(self):
        judge = make_judge(role="backup")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            title="Chronograph",
            version=3,
            bids={"buyer-a": 300.0},
        )

        response = judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    version=2,
                    bids={"buyer-b": 400.0},
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertIn("Stale version", response.message)
        self.assertNotIn("buyer-b", judge.auction_store["auction-1"].bids)

    def test_commit_merges_bids_and_overwrites_same_buyer(self):
        judge = make_judge(role="backup")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            title="Chronograph",
            reserve_price=500.0,
            version=1,
            bids={"buyer-a": 300.0},
        )

        first = judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    version=1,
                    bids={"buyer-b": 450.0},
                )
            ),
            NoopContext(),
        )
        second = judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    version=2,
                    bids={"buyer-a": 700.0},
                )
            ),
            NoopContext(),
        )

        self.assertTrue(first.success)
        self.assertTrue(second.success)
        self.assertEqual(judge.auction_store["auction-1"].version, 3)
        self.assertEqual(judge.auction_store["auction-1"].bids["buyer-a"], 700.0)
        self.assertEqual(judge.auction_store["auction-1"].bids["buyer-b"], 450.0)
        self.assertTrue(judge.auction_store["auction-1"].reserve_met)

    def test_reveal_locks_auction_against_later_bids(self):
        judge = make_judge(role="backup")
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=4,
            bids={"buyer-a": 900.0},
        )

        reveal = judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(auction_id="auction-1", version=4),
                is_reveal_event=True,
            ),
            NoopContext(),
        )
        bid_after_reveal = judge.CommitToVault(
            pb2.CommitRequest(
                auction=pb2.Auction(
                    auction_id="auction-1",
                    version=5,
                    bids={"buyer-b": 1000.0},
                )
            ),
            NoopContext(),
        )

        self.assertTrue(reveal.success)
        self.assertEqual(judge.auction_store["auction-1"].state, pb2.AUCTION_STATE_REVEALED)
        self.assertFalse(bid_after_reveal.success)
        self.assertNotIn("buyer-b", judge.auction_store["auction-1"].bids)

    def test_primary_rolls_back_when_reachable_peer_rejects_replication(self):
        judge = make_judge(role="primary", peers=["peer-a:50051"])
        judge.auction_store["auction-1"] = pb2.Auction(
            auction_id="auction-1",
            version=1,
            bids={"buyer-a": 100.0},
        )

        with mock.patch.object(judge, "_replicate_to_peers", return_value=False):
            response = judge.CommitToVault(
                pb2.CommitRequest(
                    auction=pb2.Auction(
                        auction_id="auction-1",
                        version=1,
                        bids={"buyer-b": 200.0},
                    )
                ),
                NoopContext(),
            )

        self.assertFalse(response.success)
        self.assertEqual(judge.auction_store["auction-1"].version, 1)
        self.assertNotIn("buyer-b", judge.auction_store["auction-1"].bids)

    def test_query_filters_by_id_title_and_description(self):
        judge = make_judge(role="backup")
        judge.auction_store["a-1"] = pb2.Auction(
            auction_id="a-1",
            title="Vintage Camera",
            description="Brass body",
        )
        judge.auction_store["a-2"] = pb2.Auction(
            auction_id="a-2",
            title="Modern Watch",
            description="Steel bracelet",
        )

        response = judge.QueryVault(pb2.QueryRequest(filter="brass"), NoopContext())

        self.assertTrue(response.ok)
        self.assertEqual(response.count, 1)
        self.assertEqual(response.auctions[0].auction_id, "a-1")
