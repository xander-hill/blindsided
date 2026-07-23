#!/usr/bin/env python3
"""Submit simultaneous stale-version bids through the public AuctionService API."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys
import threading
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


DASHBOARDS = ("RPC & Mutation Outcomes", "Concurrency & Replication")
METRICS = (
    "blindsided_rpc_requests_total",
    "blindsided_rpc_duration_seconds",
    "blindsided_mutations_total",
    "blindsided_concurrency_retries_total",
    "blindsided_idempotency_requests_total",
    "blindsided_replication_attempts_total",
    "blindsided_replication_duration_seconds",
    "blindsided_commits_total",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="localhost:50052")
    parser.add_argument("--bidders", type=int, default=24)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--run-id", default=str(uuid4()))
    return parser.parse_args()


def future_timestamp() -> timestamp_pb2.Timestamp:
    value = timestamp_pb2.Timestamp()
    value.FromSeconds(int(time.time()) + 3600)
    return value


def main() -> int:
    args = parse_args()
    if args.bidders < 2:
        raise ValueError("--bidders must be at least 2")
    if args.timeout <= 0:
        raise ValueError("--timeout must be positive")

    print("Expected Grafana dashboard views:")
    for dashboard in DASHBOARDS:
        print(f"  - {dashboard}")
    print("Expected affected metrics:")
    for metric in METRICS:
        print(f"  - {metric}")
    print(f"\nScenario ID: {args.run_id}")
    print(f"Bidders: {args.bidders}; AuctionService: {args.address}")

    with grpc.insecure_channel(args.address) as channel:
        try:
            grpc.channel_ready_future(channel).result(timeout=args.timeout)
        except grpc.FutureTimeoutError as error:
            raise RuntimeError(f"AuctionService at {args.address} is unavailable") from error
        stub = pb2_grpc.AuctionServiceStub(channel)
        try:
            created = stub.CreateAuction(
                pb2.CreateAuctionRequest(
                    seller_id=f"concurrency-seller-{args.run_id}",
                    title=f"Concurrent bidding {args.run_id}",
                    category="evaluation",
                    reserve_price=100.0,
                    ends_at=future_timestamp(),
                    request_id=f"{args.run_id}:create",
                ),
                timeout=args.timeout,
            )
        except grpc.RpcError as error:
            raise RuntimeError(
                f"CreateAuction RPC failed: {error.code().name}: {error.details()}"
            ) from error
        if not created.ok:
            raise RuntimeError(f"CreateAuction was not committed: {created.message}")

    barrier = threading.Barrier(args.bidders)

    def place_bid(index: int) -> tuple[int, bool, str]:
        bidder = f"evaluation-bidder-{index:03d}"
        barrier.wait(timeout=args.timeout)
        with grpc.insecure_channel(args.address) as worker_channel:
            worker = pb2_grpc.AuctionServiceStub(worker_channel)
            try:
                response = worker.PlaceBid(
                    pb2.BidRequest(
                        auction_id=created.auction_id,
                        bidder_id=bidder,
                        amount=125.0 + index,
                        expected_version=1,
                        request_id=f"{args.run_id}:bid:{index}",
                    ),
                    timeout=args.timeout,
                )
                return index, response.success, response.message
            except grpc.RpcError as error:
                return index, False, f"{error.code().name}: {error.details()}"

    results = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.bidders) as executor:
        futures = [executor.submit(place_bid, index) for index in range(args.bidders)]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started

    successes = [result for result in results if result[1]]
    failures = [result for result in results if not result[1]]
    for index, _, message in sorted(failures):
        print(f"[rejected] bidder {index}: {message}")

    with grpc.insecure_channel(args.address) as channel:
        stub = pb2_grpc.AuctionServiceStub(channel)
        try:
            final = stub.GetAuction(
                pb2.GetAuctionRequest(auction_id=created.auction_id),
                timeout=args.timeout,
            )
        except grpc.RpcError as error:
            raise RuntimeError(
                f"Final GetAuction failed: {error.code().name}: {error.details()}"
            ) from error
    if not final.ok:
        raise RuntimeError(f"Final GetAuction failed: {final.message}")
    if not successes:
        raise RuntimeError("No concurrent bids committed")
    if final.auction.bidder_count != len(successes):
        raise RuntimeError(
            f"State mismatch: {len(successes)} successful responses but "
            f"{final.auction.bidder_count} stored bidders"
        )

    print(
        f"\nCompleted in {elapsed:.3f}s: {len(successes)} committed, "
        f"{len(failures)} rejected, final bidder_count={final.auction.bidder_count}."
    )
    print(
        "Concurrency retry activity is timing-dependent; increase --bidders if "
        "blindsided_concurrency_retries_total does not move."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, threading.BrokenBarrierError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
