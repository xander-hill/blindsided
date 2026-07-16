from google.protobuf import timestamp_pb2

from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import (
    BackendTestCase,
    NoopContext,
    active_bid,
    future_timestamp,
    make_judge,
)


class AuctionCreationSpecificationTests(BackendTestCase):
    """Contract tests for docs/auction-specification.md section 2.2."""

    def test_auction_contract_defines_immutable_ends_at_timestamp(self):
        self.assertIn("ends_at", pb2.Auction.DESCRIPTOR.fields_by_name)

    def test_successful_creation_establishes_all_required_properties(self):
        judge = make_judge(role="backup")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="creation-complete",
                    seller_id="seller-a",
                    title="Complete Creation",
                    reserve_price=100.0,
                    state=pb2.AUCTION_STATE_OPEN,
                    version=99,
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        created = judge.auction_store["creation-complete"]
        self.assertEqual(created.auction_id, "creation-complete")
        self.assertEqual(created.seller_id, "seller-a")
        self.assertEqual(created.ends_at, future_timestamp())
        self.assertEqual(created.reserve_price, 100.0)
        self.assertEqual(dict(created.bids), {})
        self.assertEqual(created.state, pb2.AUCTION_STATE_OPEN)
        self.assertEqual(created.version, 1)
        self.assertFalse(created.HasField("result"))
        self.assertNotIn("reserve_met", pb2.Auction.DESCRIPTOR.fields_by_name)
        self.assertEqual(response.current_version, 1)

    def test_creation_requires_seller_identity(self):
        judge = make_judge(role="backup")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="creation-missing-seller",
                    title="Missing Seller",
                    reserve_price=100.0,
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("creation-missing-seller", judge.auction_store)

    def test_creation_requires_ends_at(self):
        judge = make_judge(role="backup")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="creation-missing-ends-at",
                    seller_id="seller-a",
                    title="Missing Ends At",
                    reserve_price=100.0,
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("creation-missing-ends-at", judge.auction_store)

    def test_creation_requires_reserve_price(self):
        judge = make_judge(role="backup")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="creation-missing-reserve",
                    seller_id="seller-a",
                    title="Missing Reserve",
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("creation-missing-reserve", judge.auction_store)

    def test_creation_starts_with_empty_active_bid_collection(self):
        judge = make_judge(role="backup")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="creation-with-bid",
                    seller_id="seller-a",
                    title="Creation With Bid",
                    reserve_price=100.0,
                    bids={"bidder-a": active_bid(125.0)},
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("creation-with-bid", judge.auction_store)

    def test_initial_version_is_assigned_by_system(self):
        judge = make_judge(role="backup")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="creation-client-version",
                    seller_id="seller-a",
                    title="Client Version",
                    reserve_price=100.0,
                    version=99,
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        self.assertTrue(response.success)
        self.assertEqual(judge.auction_store["creation-client-version"].version, 1)
        self.assertEqual(response.current_version, 1)

    def test_ends_at_is_immutable_after_creation(self):
        judge = make_judge(role="backup")
        judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="creation-immutable-ends-at",
                    seller_id="seller-a",
                    title="Immutable Ends At",
                    reserve_price=100.0,
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="creation-immutable-ends-at",
                    version=1,
                    ends_at=timestamp_pb2.Timestamp(seconds=4102531200),
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(
            judge.auction_store["creation-immutable-ends-at"].ends_at,
            future_timestamp(),
        )

    def test_auction_identifier_must_be_unique(self):
        judge = make_judge(role="backup")
        judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="creation-unique-id",
                    seller_id="seller-a",
                    title="Original",
                    reserve_price=100.0,
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="creation-unique-id",
                    seller_id="seller-b",
                    title="Duplicate",
                    reserve_price=250.0,
                    version=1,
                    ends_at=future_timestamp(),
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertEqual(judge.auction_store["creation-unique-id"].seller_id, "seller-a")
        self.assertEqual(judge.auction_store["creation-unique-id"].title, "Original")
        self.assertEqual(judge.auction_store["creation-unique-id"].version, 1)

    def test_incomplete_creation_fails_without_partial_auction(self):
        judge = make_judge(role="backup")

        response = judge.ApplyAuctionMutation(
            pb2.AuctionMutationRequest(
                auction=pb2.Auction(
                    auction_id="creation-partial",
                    reserve_price=100.0,
                )
            ),
            NoopContext(),
        )

        self.assertFalse(response.success)
        self.assertNotIn("creation-partial", judge.auction_store)
