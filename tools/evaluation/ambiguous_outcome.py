#!/usr/bin/env python3
"""Validate same-request retry after a deliberately ambiguous RPC deadline."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from uuid import uuid4

import grpc
from google.protobuf import timestamp_pb2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

from blindsided.generated import blindsided_pb2 as pb2  # noqa: E402
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc  # noqa: E402


def require(condition: bool, transition: str, detail: str) -> None:
    if not condition:
        raise RuntimeError(f"transition={transition}: {detail}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="localhost:50052")
    parser.add_argument("--timeout", type=float, default=15)
    parser.add_argument("--ambiguous-timeout", type=float, default=0.001)
    parser.add_argument("--run-id", default=str(uuid4()))
    args = parser.parse_args()

    print("Timeline:")
    with grpc.insecure_channel(args.address) as channel:
        grpc.channel_ready_future(channel).result(timeout=args.timeout)
        stub = pb2_grpc.AuctionServiceStub(channel)
        ends_at = timestamp_pb2.Timestamp()
        ends_at.FromSeconds(int(time.time()) + 3600)
        created = stub.CreateAuction(
            pb2.CreateAuctionRequest(
                seller_id=f"ambiguous-seller-{args.run_id}",
                title=f"Ambiguous outcome {args.run_id}",
                category="evaluation",
                reserve_price=100,
                ends_at=ends_at,
                request_id=f"{args.run_id}:create",
            ),
            timeout=args.timeout,
        )
        require(created.ok, "create", created.message)
        print("  READY → version 1 committed")

        request = pb2.BidRequest(
            auction_id=created.auction_id,
            bidder_id="ambiguous-bidder",
            amount=150,
            expected_version=1,
            request_id=f"{args.run_id}:ambiguous-bid",
        )
        first_known_success = False
        try:
            first = stub.PlaceBid(request, timeout=args.ambiguous_timeout)
            first_known_success = first.success
            require(first.success, "ambiguous-submit", first.message)
            print("  mutation response arrived before deadline")
        except grpc.RpcError as error:
            require(
                error.code() in (grpc.StatusCode.DEADLINE_EXCEEDED, grpc.StatusCode.CANCELLED),
                "ambiguous-submit",
                f"unexpected RPC outcome {error.code().name}: {error.details()}",
            )
            print(f"  response interrupted: {error.code().name}")

        replay = stub.PlaceBid(request, timeout=args.timeout)
        require(replay.success, "same-id-retry", replay.message)
        print("  same request ID accepted/replayed")

        conflict = stub.PlaceBid(
            pb2.BidRequest(
                auction_id=created.auction_id,
                bidder_id="ambiguous-bidder",
                amount=151,
                expected_version=2,
                request_id=request.request_id,
            ),
            timeout=args.timeout,
        )
        require(
            not conflict.success,
            "changed-payload-retry",
            "different payload with the same request ID was accepted",
        )
        print("  changed payload rejected")

        reveal = stub.RevealAuction(
            pb2.RevealAuctionRequest(
                auction_id=created.auction_id,
                seller_id=f"ambiguous-seller-{args.run_id}",
                expected_version=2,
                request_id=f"{args.run_id}:reveal",
            ),
            timeout=args.timeout,
        )
        require(
            reveal.ok and reveal.final_version == 3,
            "at-most-once-version",
            f"expected final version 3, got {reveal.final_version}: {reveal.message}",
        )
        final = stub.GetAuction(
            pb2.GetAuctionRequest(auction_id=created.auction_id),
            timeout=args.timeout,
        )
        require(
            final.ok
            and final.auction.bidder_count == 1
            and final.auction.result.winning_bidder_id == "ambiguous-bidder",
            "final-state",
            "mutation was lost or applied more than once",
        )
        print(
            "  final version=3, bidder_count=1, "
            f"first_response_known={first_known_success}"
        )

    print(
        "Metrics: inspect RPC deadline/cancellation, idempotency replay/conflict, "
        "mutation, replication, and commit panels for this interval."
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, grpc.RpcError, grpc.FutureTimeoutError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
