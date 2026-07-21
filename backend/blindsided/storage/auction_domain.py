import time

from blindsided.generated import blindsided_pb2 as pb2


class AuctionDomain:
    """Stateless auction validation, candidate construction, and result logic."""

    def build_candidate(
        self,
        request: pb2.AuctionMutationRequest,
        mutation_type: pb2.AuctionMutationType,
        existing_auction: pb2.Auction | None,
    ) -> tuple[pb2.Auction | None, pb2.AuctionMutationResponse | None]:
        """Validate in mutation-path order and return a detached candidate."""
        if existing_auction is None:
            return self.build_create_candidate(request.auction, mutation_type)

        validation_error = self.existing_mutation_error(
            request, mutation_type, existing_auction
        )
        if validation_error is not None:
            return None, validation_error

        if mutation_type == pb2.AUCTION_MUTATION_TYPE_REVEAL:
            return self.build_reveal_candidate(existing_auction), None
        if mutation_type == pb2.AUCTION_MUTATION_TYPE_PLACE_BID:
            return self.build_bid_candidate(request, existing_auction)
        if mutation_type == pb2.AUCTION_MUTATION_TYPE_WITHDRAW_BID:
            return self.build_withdraw_candidate(request, existing_auction)
        return None, self.mutation_error(
            "Unsupported auction mutation type.",
            current_version=existing_auction.version,
        )

    def build_create_candidate(
        self,
        requested_auction: pb2.Auction,
        mutation_type: pb2.AuctionMutationType,
    ) -> tuple[pb2.Auction | None, pb2.AuctionMutationResponse | None]:
        if mutation_type == pb2.AUCTION_MUTATION_TYPE_REVEAL:
            return None, self.mutation_error(
                "Cannot reveal an auction that does not exist.",
                reason=pb2.MUTATION_FAILURE_REASON_NOT_FOUND,
            )
        if mutation_type != pb2.AUCTION_MUTATION_TYPE_CREATE:
            return None, self.mutation_error(
                "Auction does not exist.",
                reason=pb2.MUTATION_FAILURE_REASON_NOT_FOUND,
            )

        validation_error = self.create_request_error(requested_auction)
        if validation_error is not None:
            return None, validation_error

        candidate = pb2.Auction()
        candidate.CopyFrom(requested_auction)
        candidate.state = pb2.AUCTION_STATE_OPEN
        candidate.version = 1
        candidate.next_bid_sequence = 1
        return candidate, None

    def create_request_error(
        self, requested_auction: pb2.Auction
    ) -> pb2.AuctionMutationResponse | None:
        if not requested_auction.auction_id.strip():
            return self.mutation_error("Auction creation requires an auction id.")
        if not requested_auction.seller_id.strip():
            return self.mutation_error("Auction creation requires a seller id.")
        if not requested_auction.HasField("ends_at"):
            return self.mutation_error(
                "Auction creation requires an immutable closing timestamp."
            )
        if requested_auction.reserve_price <= 0:
            return self.mutation_error(
                "Auction creation requires a positive reserve price."
            )
        if requested_auction.bids:
            return self.mutation_error(
                "Auction creation must start with no active bids."
            )
        if requested_auction.state == pb2.AUCTION_STATE_REVEALED:
            return self.mutation_error("Auction creation must begin open.")
        if requested_auction.HasField("result"):
            return self.mutation_error(
                "An open auction cannot have a committed result."
            )
        return None

    def existing_mutation_error(
        self,
        request: pb2.AuctionMutationRequest,
        mutation_type: pb2.AuctionMutationType,
        existing_auction: pb2.Auction,
    ) -> pb2.AuctionMutationResponse | None:
        """Validate stored state before immutable fields and expected version."""
        current_version = existing_auction.version
        if existing_auction.state == pb2.AUCTION_STATE_REVEALED:
            return self.mutation_error(
                "The Gavel has already fallen.", current_version=current_version
            )
        if (
            request.auction.state == pb2.AUCTION_STATE_REVEALED
            and mutation_type != pb2.AUCTION_MUTATION_TYPE_REVEAL
        ):
            return self.mutation_error(
                "Reveal requires a reveal event.", current_version=current_version
            )

        state_error = self.acceptance_order_state_error(existing_auction)
        if state_error:
            return self.mutation_error(state_error, current_version=current_version)
        if (
            mutation_type != pb2.AUCTION_MUTATION_TYPE_REVEAL
            and self.includes_creation_metadata(request.auction)
        ):
            return self.mutation_error(
                "Auction creation properties are immutable.",
                current_version=current_version,
            )

        expected_version = request.expected_version or request.auction.version
        if expected_version != current_version:
            return self.mutation_error(
                "Fog conflict: Stale version.",
                reason=pb2.MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT,
                current_version=current_version,
            )
        return None

    def build_bid_candidate(
        self,
        request: pb2.AuctionMutationRequest,
        existing_auction: pb2.Auction,
    ) -> tuple[pb2.Auction | None, pb2.AuctionMutationResponse | None]:
        current_version = existing_auction.version
        requested_bids = request.auction.bids
        if not requested_bids:
            return None, self.mutation_error(
                "Bid mutation requires at least one bid.",
                current_version=current_version,
            )
        if len(requested_bids) != 1:
            return None, self.mutation_error(
                "Bid mutation requires exactly one bidder.",
                current_version=current_version,
            )

        bidder_id, requested_bid = next(iter(requested_bids.items()))
        if request.bidder_id.strip() and request.bidder_id != bidder_id:
            return None, self.mutation_error(
                "Bid mutation bidder id must match its single bid.",
                current_version=current_version,
            )
        if self.auction_has_ended(existing_auction):
            return None, self.mutation_error(
                "Auction deadline has passed.", current_version=current_version
            )

        current_bid = existing_auction.bids.get(bidder_id)
        if current_bid is not None and requested_bid.amount <= current_bid.amount:
            return None, self.mutation_error(
                "Bid must be higher than bidder's active bid.",
                current_version=current_version,
            )

        candidate = pb2.Auction()
        candidate.CopyFrom(existing_auction)
        next_bid_sequence = self.next_bid_sequence(existing_auction)
        candidate.bids[bidder_id].CopyFrom(
            pb2.ActiveBid(
                amount=requested_bid.amount,
                acceptance_order=next_bid_sequence,
            )
        )
        candidate.next_bid_sequence = next_bid_sequence + 1
        candidate.version = current_version + 1
        return candidate, None

    def build_withdraw_candidate(
        self,
        request: pb2.AuctionMutationRequest,
        existing_auction: pb2.Auction,
    ) -> tuple[pb2.Auction | None, pb2.AuctionMutationResponse | None]:
        current_version = existing_auction.version
        if self.auction_has_ended(existing_auction):
            return None, self.mutation_error(
                "Auction deadline has passed.", current_version=current_version
            )

        bidder_id = request.bidder_id.strip()
        if not bidder_id:
            return None, self.mutation_error(
                "Withdrawal requires a bidder id.", current_version=current_version
            )
        if bidder_id not in existing_auction.bids:
            return None, self.mutation_error(
                "Bidder has no active bid to withdraw.",
                current_version=current_version,
            )

        candidate = pb2.Auction()
        candidate.CopyFrom(existing_auction)
        candidate.next_bid_sequence = self.next_bid_sequence(existing_auction)
        del candidate.bids[bidder_id]
        candidate.version = current_version + 1
        return candidate, None

    def build_result(self, auction: pb2.Auction) -> pb2.AuctionResult:
        """Calculate the committed result using amount then acceptance order."""
        state_error = self.acceptance_order_state_error(auction)
        if state_error:
            raise ValueError(state_error)
        if not auction.bids:
            return pb2.AuctionResult(
                outcome=pb2.AUCTION_OUTCOME_NO_BIDS,
                reserve_met=False,
                has_winner=False,
            )
        winning_bidder_id, winning_bid = min(
            auction.bids.items(),
            key=lambda item: (-item[1].amount, item[1].acceptance_order, item[0]),
        )
        if winning_bid.amount < auction.reserve_price:
            return pb2.AuctionResult(
                outcome=pb2.AUCTION_OUTCOME_RESERVE_NOT_MET,
                reserve_met=False,
                has_winner=False,
            )
        return pb2.AuctionResult(
            outcome=pb2.AUCTION_OUTCOME_SUCCESSFUL_SALE,
            reserve_met=True,
            has_winner=True,
            winning_bidder_id=winning_bidder_id,
            winning_amount=winning_bid.amount,
        )

    def build_reveal_candidate(self, auction: pb2.Auction) -> pb2.Auction:
        candidate = pb2.Auction()
        candidate.CopyFrom(auction)
        candidate.state = pb2.AUCTION_STATE_REVEALED
        candidate.version = auction.version + 1
        candidate.result.CopyFrom(self.build_result(candidate))
        return candidate

    def next_bid_sequence(self, auction: pb2.Auction) -> int:
        if auction.next_bid_sequence > 0:
            return auction.next_bid_sequence
        if not auction.bids:
            return 1
        return max(bid.acceptance_order for bid in auction.bids.values()) + 1

    def acceptance_order_state_error(self, auction: pb2.Auction) -> str:
        active_orders = [
            bid.acceptance_order
            for bid in auction.bids.values()
            if bid.acceptance_order > 0
        ]
        if len(active_orders) != len(set(active_orders)):
            return "Corrupted auction state: duplicate acceptance order."
        if auction.next_bid_sequence > 0 and active_orders:
            if auction.next_bid_sequence < max(active_orders) + 1:
                return "Corrupted auction state: next bid sequence is stale."
        return ""

    def committed_state_error(self, auction: pb2.Auction) -> str:
        """Return why persisted or replicated auction state is inconsistent."""
        state_error = self.acceptance_order_state_error(auction)
        if state_error:
            return state_error
        if auction.state != pb2.AUCTION_STATE_REVEALED:
            if auction.HasField("result"):
                return "An open auction cannot have a committed result."
            return ""
        if not auction.HasField("result"):
            return "A revealed auction must have a committed result."
        if auction.result != self.build_result(auction):
            return "A revealed auction result does not match its committed bids."
        return ""

    def auction_has_ended(self, auction: pb2.Auction) -> bool:
        if not auction.HasField("ends_at"):
            return False
        deadline = auction.ends_at.seconds + (auction.ends_at.nanos / 1_000_000_000)
        return time.time() >= deadline

    @staticmethod
    def includes_creation_metadata(auction: pb2.Auction) -> bool:
        return (
            bool(auction.seller_id.strip())
            or bool(auction.title.strip())
            or bool(auction.category.strip())
            or bool(auction.description.strip())
            or auction.reserve_price > 0
            or auction.HasField("ends_at")
        )

    @staticmethod
    def mutation_error(
        message: str,
        *,
        reason=pb2.MUTATION_FAILURE_REASON_INVALID_STATE,
        current_version: int = 0,
    ) -> pb2.AuctionMutationResponse:
        return pb2.AuctionMutationResponse(
            success=False,
            current_version=current_version,
            failure_reason=reason,
            message=message,
        )
