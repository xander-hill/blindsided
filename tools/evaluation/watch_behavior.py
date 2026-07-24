#!/usr/bin/env python3
"""Validate multi-watcher privacy, commit filtering, cancellation, and failover."""

from __future__ import annotations

import argparse
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
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


def query(url: str, promql: str) -> list[dict]:
    endpoint = f"{url}/api/v1/query?{urllib.parse.urlencode({'query': promql})}"
    with urllib.request.urlopen(endpoint, timeout=5) as response:
        return __import__("json").load(response)["data"]["result"]


def metric(url: str, promql: str) -> float:
    return sum(float(item["value"][1]) for item in query(url, promql))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="localhost:50052")
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument(
        "--compose-file", default=str(ROOT / "deploy/compose/docker-compose.yaml")
    )
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--run-id", default=str(uuid4()))
    args = parser.parse_args()
    compose = ["docker", "compose", "-f", args.compose_file]
    stopped_service = ""

    print("Timeline:")
    channel = grpc.insecure_channel(args.address)
    try:
        grpc.channel_ready_future(channel).result(timeout=15)
        stub = pb2_grpc.AuctionServiceStub(channel)
        synchronized_backups = query(
            args.prometheus_url,
            '(blindsided_storage_role{job="storage",role="backup"} == 1) '
            'and on(instance) (blindsided_storage_ready{job="storage"} == 1)',
        )
        require(
            len(synchronized_backups) == 1,
            "ready-protected-topology",
            f"expected one synchronized backup, found {len(synchronized_backups)}",
        )
        ends_at = timestamp_pb2.Timestamp()
        ends_at.FromSeconds(int(time.time()) + 3600)
        seller = f"watch-seller-{args.run_id}"
        created = stub.CreateAuction(
            pb2.CreateAuctionRequest(
                seller_id=seller,
                title=f"Watch behavior {args.run_id}",
                category="evaluation",
                reserve_price=200,
                ends_at=ends_at,
                request_id=f"{args.run_id}:create",
            ),
            timeout=15,
        )
        require(created.ok, "create", created.message)
        updates: list[queue.Queue] = [queue.Queue(), queue.Queue()]
        streams = []

        def consume(index: int) -> None:
            try:
                stream = stub.WatchAuction(
                    pb2.AuctionRequest(
                        auction_id=created.auction_id, user_id=f"watcher-{index}"
                    ),
                    timeout=args.timeout + 60,
                )
                streams.append(stream)
                for update in stream:
                    updates[index].put(update)
            except grpc.RpcError as error:
                updates[index].put(error)

        threads = [
            threading.Thread(target=consume, args=(index,), daemon=True)
            for index in range(2)
        ]
        active_before = metric(
            args.prometheus_url, "sum(blindsided_active_watch_streams) or vector(0)"
        )
        emitted_before = metric(
            args.prometheus_url,
            "sum(blindsided_watch_updates_total) or vector(0)",
        )
        for thread in threads:
            thread.start()

        deadline = time.monotonic() + 15
        while metric(
            args.prometheus_url, "sum(blindsided_active_watch_streams) or vector(0)"
        ) < active_before + 2:
            require(time.monotonic() < deadline, "watch-open", "two streams not active")
            time.sleep(0.5)
        print("  two watchers active")

        def expect(version: int, bidder_count: int) -> None:
            for watcher_updates in updates:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    item = watcher_updates.get(timeout=max(0.01, deadline - time.monotonic()))
                    require(not isinstance(item, grpc.RpcError), "watch-update", str(item))
                    require(
                        not item.HasField("result") if item.state == pb2.AUCTION_STATE_OPEN else True,
                        "watch-privacy",
                        "open update exposed result/reserve/winner",
                    )
                    if item.version == version:
                        require(
                            item.bidder_count == bidder_count,
                            "watch-state",
                            f"version {version} bidder_count={item.bidder_count}",
                        )
                        break
                else:
                    raise RuntimeError(f"transition=watch-update: missing version {version}")

        expect(1, 0)
        bid_request = pb2.BidRequest(
            auction_id=created.auction_id,
            bidder_id="watch-bidder",
            amount=250,
            expected_version=1,
            request_id=f"{args.run_id}:bid",
        )
        bid = stub.PlaceBid(bid_request, timeout=15)
        require(bid.success, "committed-mutation", bid.message)
        expect(2, 1)
        emitted_after_commit = metric(
            args.prometheus_url, "sum(blindsided_watch_updates_total) or vector(0)"
        )

        conflict = stub.PlaceBid(
            pb2.BidRequest(
                auction_id=created.auction_id,
                bidder_id="watch-bidder",
                amount=251,
                expected_version=2,
                request_id=bid_request.request_id,
            ),
            timeout=15,
        )
        require(not conflict.success, "rejected-mutation", "conflict was accepted")
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            require(
                all(item.empty() for item in updates),
                "commit-filter",
                "rejected mutation emitted a watch update",
            )
            time.sleep(0.25)
        require(
            metric(args.prometheus_url, "sum(blindsided_watch_updates_total) or vector(0)")
            == emitted_after_commit,
            "commit-filter-metric",
            "watch update counter moved after rejection",
        )
        print("  rejected mutation emitted no update")

        primary = query(
            args.prometheus_url,
            'blindsided_storage_role{job="storage",role="primary"} == 1',
        )
        require(len(primary) == 1, "discover-primary", f"found {len(primary)}")
        old_epoch = metric(
            args.prometheus_url, 'blindsided_primary_epoch{job="controller"}'
        )
        stopped_service = primary[0]["metric"]["instance"].split(":")[0]
        subprocess.run([*compose, "stop", stopped_service], check=True)
        print(f"  stopped runtime primary {stopped_service}; streams remain connected")

        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            ready = metric(
                args.prometheus_url, 'blindsided_cluster_ready{job="controller"}'
            )
            epoch = metric(
                args.prometheus_url, 'blindsided_primary_epoch{job="controller"}'
            )
            if ready == 1 and epoch > old_epoch:
                break
            time.sleep(1)
        else:
            raise RuntimeError("transition=watch-failover: readiness did not recover")

        post_failover = stub.PlaceBid(
            pb2.BidRequest(
                auction_id=created.auction_id,
                bidder_id="watch-bidder-2",
                amount=240,
                expected_version=2,
                request_id=f"{args.run_id}:post-failover",
            ),
            timeout=15,
        )
        require(post_failover.success, "post-failover-write", post_failover.message)
        expect(3, 2)
        reveal = stub.RevealAuction(
            pb2.RevealAuctionRequest(
                auction_id=created.auction_id,
                seller_id=seller,
                expected_version=3,
                request_id=f"{args.run_id}:reveal",
            ),
            timeout=15,
        )
        require(reveal.ok and reveal.final_version == 4, "reveal", reveal.message)
        expect(4, 2)
        for stream in streams:
            stream.cancel()
        for thread in threads:
            thread.join(timeout=10)
        deadline = time.monotonic() + 15
        while metric(
            args.prometheus_url, "sum(blindsided_active_watch_streams) or vector(0)"
        ) != active_before:
            require(time.monotonic() < deadline, "watch-cancel", "active streams leaked")
            time.sleep(0.5)
        print("  watchers crossed failover, revealed permitted outcome, and closed")
        print(
            f"  watch updates delta="
            f"{int(metric(args.prometheus_url, 'sum(blindsided_watch_updates_total) or vector(0)') - emitted_before)}"
        )
    finally:
        channel.close()
        if stopped_service:
            subprocess.run([*compose, "start", stopped_service], check=False)
            recovery_deadline = time.monotonic() + args.timeout
            while time.monotonic() < recovery_deadline:
                registered = metric(
                    args.prometheus_url,
                    'blindsided_registered_replicas{job="controller"}',
                )
                healthy = metric(
                    args.prometheus_url,
                    'blindsided_healthy_replicas{job="controller"}',
                )
                ready_primary = query(
                    args.prometheus_url,
                    '(blindsided_storage_role{job="storage",role="primary"} == 1) '
                    'and on(instance) (blindsided_storage_ready{job="storage"} == 1)',
                )
                ready_backup = query(
                    args.prometheus_url,
                    '(blindsided_storage_role{job="storage",role="backup"} == 1) '
                    'and on(instance) (blindsided_storage_ready{job="storage"} == 1)',
                )
                active_streams = metric(
                    args.prometheus_url,
                    "sum(blindsided_active_watch_streams) or vector(0)",
                )
                if (
                    registered == 3
                    and healthy == 3
                    and len(ready_primary) == 1
                    and len(ready_backup) == 1
                    and active_streams == 0
                ):
                    break
                time.sleep(1)
            else:
                raise RuntimeError(
                    "transition=watch-cleanup: restored replica/recovery work "
                    "did not settle before deadline"
                )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, grpc.RpcError, subprocess.CalledProcessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
