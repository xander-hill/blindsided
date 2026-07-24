#!/usr/bin/env python3
"""Generate a deterministic, public-API auction lifecycle for observability demos."""

from __future__ import annotations

import argparse
from pathlib import Path
import queue
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

        print("\nTimeline:")
        updates: queue.Queue = queue.Queue()
        watch_stop = threading.Event()

        def watch() -> None:
            try:
                stream = stub.WatchAuction(
                    pb2.AuctionRequest(auction_id=auction_id, user_id="demo-watcher"),
                    timeout=args.timeout,
                )
                for update in stream:
                    updates.put(update)
                    if update.state == pb2.AUCTION_STATE_REVEALED:
                        return
                    if watch_stop.is_set():
                        stream.cancel()
                        return
            except grpc.RpcError as error:
                if not watch_stop.is_set():
                    updates.put(error)

        watcher = threading.Thread(target=watch, daemon=True)
        watcher.start()

        committed_versions = []

        def await_version(version: int):
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline:
                try:
                    update = updates.get(timeout=max(0.01, deadline - time.monotonic()))
                except queue.Empty as error:
                    raise RuntimeError(
                        f"watch did not observe committed version {version}"
                    ) from error
                if isinstance(update, grpc.RpcError):
                    raise RuntimeError(
                        f"watch failed: {update.code().name}: {update.details()}"
                    )
                require(
                    update.version in set(committed_versions) | {version},
                    f"watch emitted uncommitted version {update.version}",
                )
                require(
                    not update.HasField("result")
                    if update.state == pb2.AUCTION_STATE_OPEN
                    else True,
                    "pre-reveal watch update exposed auction outcome",
                )
                print(
                    f"  watch version={update.version} "
                    f"bidders={update.bidder_count} state={update.state}"
                )
                if update.version == version:
                    return update
            raise RuntimeError(f"watch deadline expired for version {version}")

        committed_versions.append(1)
        initial = await_version(1)
        require(initial.bidder_count == 0, "created auction has unexpected bidders")

        mutations = (
            (
                "bid bidder-a",
                stub.PlaceBid,
                pb2.BidRequest(
                    auction_id=auction_id,
                    bidder_id="bidder-a",
                    amount=125.0,
                    expected_version=1,
                    request_id=f"{args.run_id}:bid:a",
                ),
                2,
                1,
            ),
            (
                "bid bidder-b",
                stub.PlaceBid,
                pb2.BidRequest(
                    auction_id=auction_id,
                    bidder_id="bidder-b",
                    amount=160.0,
                    expected_version=2,
                    request_id=f"{args.run_id}:bid:b",
                ),
                3,
                2,
            ),
            (
                "replace bidder-a",
                stub.PlaceBid,
                pb2.BidRequest(
                    auction_id=auction_id,
                    bidder_id="bidder-a",
                    amount=175.0,
                    expected_version=3,
                    request_id=f"{args.run_id}:replace:a",
                ),
                4,
                2,
            ),
        )
        for label, rpc, request, version, bidder_count in mutations:
            response = call(label, rpc, request, args.timeout)
            require(response.success, f"{label} failed: {response.message}")
            committed_versions.append(version)
            update = await_version(version)
            require(
                update.bidder_count == bidder_count,
                f"{label} produced bidder_count={update.bidder_count}",
            )

        conflict = call(
            "reject changed idempotency payload",
            stub.PlaceBid,
            pb2.BidRequest(
                auction_id=auction_id,
                bidder_id="bidder-a",
                amount=180.0,
                expected_version=4,
                request_id=f"{args.run_id}:replace:a",
            ),
            args.timeout,
        )
        require(not conflict.success, "changed idempotency payload was accepted")

        withdrawn = call(
            "withdraw bidder-b",
            stub.WithdrawBid,
            pb2.WithdrawBidRequest(
                auction_id=auction_id,
                bidder_id="bidder-b",
                expected_version=4,
                request_id=f"{args.run_id}:withdraw:b",
            ),
            args.timeout,
        )
        require(withdrawn.success and withdrawn.final_version == 5, withdrawn.message)
        committed_versions.append(5)
        require(await_version(5).bidder_count == 1, "withdraw did not remove bidder-b")

        before_reveal = call(
            "privacy read",
            stub.GetAuction,
            pb2.GetAuctionRequest(auction_id=auction_id, bidder_id="bidder-a"),
            args.timeout,
        )
        require(before_reveal.ok, before_reveal.message)
        require(before_reveal.HasField("own_active_bid_amount"), "own bid is hidden")
        require(
            abs(before_reveal.own_active_bid_amount - 175.0) < 0.001,
            "replacement bid amount is incorrect",
        )
        require(
            not before_reveal.auction.HasField("result"),
            "pre-reveal public auction exposed result or reserve outcome",
        )

        revealed = call(
            "reveal",
            stub.RevealAuction,
            pb2.RevealAuctionRequest(
                auction_id=auction_id,
                seller_id=f"evaluation-seller-{args.run_id}",
                expected_version=5,
                request_id=f"{args.run_id}:reveal",
            ),
            args.timeout,
        )
        require(revealed.ok and revealed.final_version == 6, revealed.message)
        committed_versions.append(6)
        reveal_update = await_version(6)
        require(reveal_update.HasField("result"), "reveal watch update has no result")
        require(reveal_update.result.reserve_met, "reserve should be met")
        require(reveal_update.result.has_winner, "reveal has no winner")
        require(
            reveal_update.result.winning_bidder_id == "bidder-a"
            and abs(reveal_update.result.winning_amount - 175.0) < 0.001,
            "reveal chose the wrong winner or amount",
        )

        final = call(
            "GetAuction(final)",
            stub.GetAuction,
            pb2.GetAuctionRequest(auction_id=auction_id),
            args.timeout,
        )
        require(final.ok and final.auction.HasField("result"), final.message)
        require(final.auction.bidder_count == 1, "final bidder count is not one")
        require(
            final.auction.result == reveal_update.result,
            "final read and watch reveal outcomes differ",
        )
        watch_stop.set()
        watcher.join(timeout=args.timeout)
        require(not watcher.is_alive(), "watch stream did not terminate after reveal")

    print("\nAuction lifecycle completed: versions 1→6, privacy preserved.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
