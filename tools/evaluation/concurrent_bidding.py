#!/usr/bin/env python3
"""Submit simultaneous stale-version bids through the public AuctionService API."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
import sys
import threading
import time
import urllib.parse
import urllib.request
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
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    return parser.parse_args()


def metric(url: str, query: str) -> float:
    encoded = urllib.parse.urlencode({"query": query})
    with urllib.request.urlopen(
        f"{url}/api/v1/query?{encoded}", timeout=5
    ) as response:
        result = __import__("json").load(response)["data"]["result"]
    return sum(float(item["value"][1]) for item in result)


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
    for metric_name in METRICS:
        print(f"  - {metric_name}")
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

    retry_before = metric(
        args.prometheus_url,
        'sum(blindsided_concurrency_retries_total{outcome="retried"}) or vector(0)',
    )
    conflict_before = metric(
        args.prometheus_url,
        'sum(blindsided_concurrency_retries_total{outcome="exhausted"}) or vector(0)',
    )

    def place_bid(index: int) -> tuple[int, bool, str, float]:
        bidder = f"evaluation-bidder-{index:03d}"
        barrier.wait(timeout=args.timeout)
        started = time.perf_counter()
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
                return index, response.success, response.message, time.perf_counter() - started
            except grpc.RpcError as error:
                return (
                    index,
                    False,
                    f"{error.code().name}: {error.details()}",
                    time.perf_counter() - started,
                )

    results = []
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.bidders) as executor:
        futures = [executor.submit(place_bid, index) for index in range(args.bidders)]
        for future in as_completed(futures):
            results.append(future.result())
    elapsed = time.perf_counter() - started

    successes = [result for result in results if result[1]]
    failures = [result for result in results if not result[1]]
    for index, _, message, _ in sorted(failures):
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

    expected_version = 1 + len(successes)
    if len(successes) < 2:
        raise RuntimeError("Fewer than two bidders committed; cannot verify tie-breaking")
    tie_indexes = sorted(result[0] for result in successes)[:2]
    tie_requests = []
    with grpc.insecure_channel(args.address) as channel:
        stub = pb2_grpc.AuctionServiceStub(channel)
        for offset, index in enumerate(tie_indexes):
            request = pb2.BidRequest(
                auction_id=created.auction_id,
                bidder_id=f"evaluation-bidder-{index:03d}",
                amount=1000.0,
                expected_version=expected_version + offset,
                request_id=f"{args.run_id}:tie:{index}",
            )
            response = stub.PlaceBid(request, timeout=args.timeout)
            if not response.success:
                raise RuntimeError(f"tie setup failed: {response.message}")
            tie_requests.append(request)

        for request in tie_requests:
            replay = stub.PlaceBid(request, timeout=args.timeout)
            if not replay.success:
                raise RuntimeError(f"idempotency replay failed: {replay.message}")

        reveal = stub.RevealAuction(
            pb2.RevealAuctionRequest(
                auction_id=created.auction_id,
                seller_id=f"concurrency-seller-{args.run_id}",
                expected_version=expected_version + 2,
                request_id=f"{args.run_id}:reveal",
            ),
            timeout=args.timeout,
        )
        if not reveal.ok or reveal.final_version != expected_version + 3:
            raise RuntimeError(
                f"final version mismatch: expected {expected_version + 3}, "
                f"got {reveal.final_version}"
            )
        revealed = stub.GetAuction(
            pb2.GetAuctionRequest(auction_id=created.auction_id),
            timeout=args.timeout,
        )
    expected_winner = f"evaluation-bidder-{tie_indexes[0]:03d}"
    if (
        not revealed.ok
        or not revealed.auction.HasField("result")
        or revealed.auction.result.winning_bidder_id != expected_winner
    ):
        raise RuntimeError("equal bids did not use deterministic acceptance order")

    metric_deadline = time.monotonic() + min(args.timeout, 15)
    while True:
        retry_after = metric(
            args.prometheus_url,
            'sum(blindsided_concurrency_retries_total{outcome="retried"}) or vector(0)',
        )
        conflict_after = metric(
            args.prometheus_url,
            'sum(blindsided_concurrency_retries_total{outcome="exhausted"}) or vector(0)',
        )
        if (
            retry_after > retry_before
            or conflict_after > conflict_before
            or time.monotonic() >= metric_deadline
        ):
            break
        time.sleep(0.5)
    latencies = sorted(result[3] for result in results)
    p95 = latencies[min(len(latencies) - 1, int(len(latencies) * 0.95))]

    print(
        f"\nCompleted in {elapsed:.3f}s: {len(successes)} committed, "
        f"{len(failures)} rejected, final bidder_count={final.auction.bidder_count}, "
        f"p95={p95 * 1000:.1f}ms."
    )
    print(
        f"Retries={int(retry_after - retry_before)}, "
        f"conflicts={len(failures)} "
        f"(metric exhausted={int(conflict_after - conflict_before)}), "
        f"deterministic winner={expected_winner}, final_version={reveal.final_version}."
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError, threading.BrokenBarrierError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
