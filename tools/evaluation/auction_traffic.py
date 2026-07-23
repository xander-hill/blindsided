#!/usr/bin/env python3
"""Generate a deterministic, public-API auction lifecycle for observability demos."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from uuid import uuid4

try:
    import grpc
    from google.protobuf import timestamp_pb2
except ModuleNotFoundError as error:
    print(
        f"ERROR: Missing Python dependency '{error.name}'. "
        "Install repository requirements with: "
        "python3 -m pip install -r requirements.txt",
        file=sys.stderr,
    )
    raise SystemExit(2) from error


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPOSITORY_ROOT / "backend"))

from blindsided.generated import blindsided_pb2 as pb2  # noqa: E402
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc  # noqa: E402


DASHBOARDS = ("RPC & Mutation Outcomes", "Replication & Commit Health")
METRICS = (
    "blindsided_rpc_requests_total",
    "blindsided_rpc_duration_seconds",
    "blindsided_mutations_total",
    "blindsided_idempotency_requests_total",
    "blindsided_replication_attempts_total",
    "blindsided_replication_duration_seconds",
    "blindsided_commits_total",
    "blindsided_cluster_ready",
)


def print_observability() -> None:
    print("Expected Grafana dashboard views:")
    for dashboard in DASHBOARDS:
        print(f"  - {dashboard}")
    print("Expected affected metrics:")
    for metric in METRICS:
        print(f"  - {metric}")


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def call(label: str, rpc, request, timeout: float, *, announce: bool = True):
    try:
        response = rpc(request, timeout=timeout)
    except grpc.RpcError as error:
        details = error.details() or "no server details"
        raise RuntimeError(
            f"{label} RPC failed: {error.code().name}: {details}"
        ) from error
    if announce:
        print(f"[ok] {label}")
    return response


def future_timestamp() -> timestamp_pb2.Timestamp:
    timestamp = timestamp_pb2.Timestamp()
    timestamp.FromSeconds(int(time.time()) + 3600)
    return timestamp


def confirm_created(response) -> str:
    require(
        response.ok,
        "CreateAuction was not committed: "
        f"{response.message} (retryable={response.retryable}, "
        f"outcome_unknown={response.outcome_unknown})",
    )
    require(bool(response.auction_id), "CreateAuction returned no auction_id")
    print("[ok] CreateAuction")
    print(f"     auction_id={response.auction_id}")
    return response.auction_id


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--address",
        default="localhost:50052",
        help="AuctionService gRPC address (default: %(default)s)",
    )
    parser.add_argument(
        "--run-id",
        default=str(uuid4()),
        help="Stable scenario ID; reuse it to replay the same idempotency keys",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=15.0,
        help="Per-RPC timeout in seconds (default: %(default)s)",
    )
    parser.add_argument(
        "--create-only",
        action="store_true",
        help="Only attempt CreateAuction (used by failure scenarios)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    print_observability()
    print(f"\nScenario ID: {args.run_id}")
    print(f"AuctionService: {args.address}")

    with grpc.insecure_channel(args.address) as channel:
        try:
            grpc.channel_ready_future(channel).result(timeout=args.timeout)
        except grpc.FutureTimeoutError as error:
            raise RuntimeError(
                f"AuctionService at {args.address} did not become ready within "
                f"{args.timeout:g}s"
            ) from error
        stub = pb2_grpc.AuctionServiceStub(channel)

        created = call(
            "CreateAuction",
            stub.CreateAuction,
            pb2.CreateAuctionRequest(
                seller_id=f"evaluation-seller-{args.run_id}",
                title=f"Observability evaluation {args.run_id}",
                category="evaluation",
                description="Manual observability validation workload",
                reserve_price=100.0,
                ends_at=future_timestamp(),
                request_id=f"{args.run_id}:create",
            ),
            args.timeout,
            announce=False,
        )
        auction_id = confirm_created(created)

        if args.create_only:
            print("\nCreate-only scenario completed successfully.")
            return 0

        fetched = call(
            "GetAuction",
            stub.GetAuction,
            pb2.GetAuctionRequest(auction_id=auction_id, bidder_id="bidder-a"),
            args.timeout,
        )
        require(fetched.ok, f"GetAuction failed: {fetched.message}")

        searched = call(
            "SearchAuctions",
            stub.SearchAuctions,
            pb2.SearchAuctionsRequest(query="Observability", category="evaluation"),
            args.timeout,
        )
        require(searched.ok, f"SearchAuctions failed: {searched.message}")
        require(
            any(item.auction_id == auction_id for item in searched.auctions),
            "SearchAuctions did not return the created auction",
        )

        first_bid = call(
            "PlaceBid(first)",
            stub.PlaceBid,
            pb2.BidRequest(
                auction_id=auction_id,
                bidder_id="bidder-a",
                amount=125.0,
                expected_version=1,
                request_id=f"{args.run_id}:bid:1",
            ),
            args.timeout,
        )
        require(first_bid.success, f"First bid failed: {first_bid.message}")

        withdrawn = call(
            "WithdrawBid",
            stub.WithdrawBid,
            pb2.WithdrawBidRequest(
                auction_id=auction_id,
                bidder_id="bidder-a",
                expected_version=2,
                request_id=f"{args.run_id}:withdraw",
            ),
            args.timeout,
        )
        require(withdrawn.success, f"WithdrawBid failed: {withdrawn.message}")

        second_bid = call(
            "PlaceBid(second)",
            stub.PlaceBid,
            pb2.BidRequest(
                auction_id=auction_id,
                bidder_id="bidder-b",
                amount=150.0,
                expected_version=3,
                request_id=f"{args.run_id}:bid:2",
            ),
            args.timeout,
        )
        require(second_bid.success, f"Second bid failed: {second_bid.message}")

        revealed = call(
            "RevealAuction",
            stub.RevealAuction,
            pb2.RevealAuctionRequest(
                auction_id=auction_id,
                seller_id=f"evaluation-seller-{args.run_id}",
                expected_version=4,
                request_id=f"{args.run_id}:reveal",
            ),
            args.timeout,
        )
        require(revealed.ok, f"RevealAuction failed: {revealed.message}")

        final = call(
            "GetAuction(final)",
            stub.GetAuction,
            pb2.GetAuctionRequest(auction_id=auction_id, bidder_id="bidder-b"),
            args.timeout,
        )
        require(final.ok, f"Final GetAuction failed: {final.message}")
        require(
            final.auction.state == pb2.AUCTION_STATE_REVEALED,
            "Final auction state is not REVEALED",
        )

    print("\nAuction lifecycle completed successfully.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
