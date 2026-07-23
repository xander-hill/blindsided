#!/usr/bin/env python3
"""Restart all Compose storage services and verify durable auction semantics."""

from __future__ import annotations

import argparse
import base64
import json
from pathlib import Path
import subprocess
import sys
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


def query(prometheus: str, promql: str) -> list[dict]:
    url = f"{prometheus}/api/v1/query?{urllib.parse.urlencode({'query': promql})}"
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)["data"]["result"]


def compose(compose_file: str, *args: str, capture: bool = False) -> str:
    result = subprocess.run(
        ["docker", "compose", "-f", compose_file, *args],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=capture,
    )
    return result.stdout.strip() if capture else ""


def storage_services(compose_file: str) -> list[str]:
    services = compose(compose_file, "config", "--services", capture=True).splitlines()
    result = [service for service in services if service.startswith("storage-")]
    require(len(result) == 3, "discover-replicas", f"expected 3 storage services, got {result}")
    return result


def snapshot(compose_file: str, service: str, auction_id: str) -> pb2.Auction:
    encoded = compose(
        compose_file,
        "exec",
        "-T",
        service,
        "python",
        "-c",
        (
            "import base64,sys;"
            "from blindsided.generated import blindsided_pb2 as p;"
            "s=p.StorageSnapshot();"
            "s.ParseFromString(open('/var/lib/blindsided/auction-state.pb','rb').read());"
            "a=next(x for x in s.auctions if x.auction_id==sys.argv[1]);"
            "print(base64.b64encode(a.SerializeToString(deterministic=True)).decode())"
        ),
        auction_id,
        capture=True,
    )
    auction = pb2.Auction()
    auction.ParseFromString(base64.b64decode(encoded))
    return auction


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

    services = storage_services(args.compose_file)
    print("Timeline:")
    primary = query(
        args.prometheus_url,
        'blindsided_storage_role{job="storage",role="primary"} == 1',
    )
    backup = query(
        args.prometheus_url,
        '(blindsided_storage_role{job="storage",role="backup"} == 1) '
        'and on(instance) (blindsided_storage_ready{job="storage"} == 1)',
    )
    require(
        len(primary) == len(backup) == 1,
        "ready-protected-topology",
        "expected one primary and synchronized backup",
    )
    with grpc.insecure_channel(args.address) as channel:
        grpc.channel_ready_future(channel).result(timeout=15)
        stub = pb2_grpc.AuctionServiceStub(channel)
        ends_at = timestamp_pb2.Timestamp()
        ends_at.FromSeconds(int(time.time()) + 7200)
        seller = f"restart-seller-{args.run_id}"
        created = stub.CreateAuction(
            pb2.CreateAuctionRequest(
                seller_id=seller,
                title=f"Restart durability {args.run_id}",
                category="evaluation",
                reserve_price=150,
                ends_at=ends_at,
                request_id=f"{args.run_id}:create",
            ),
            timeout=15,
        )
        require(created.ok, "create", created.message)
        mutations = [
            pb2.BidRequest(
                auction_id=created.auction_id,
                bidder_id="bidder-a",
                amount=160,
                expected_version=1,
                request_id=f"{args.run_id}:bid-a",
            ),
            pb2.BidRequest(
                auction_id=created.auction_id,
                bidder_id="bidder-b",
                amount=170,
                expected_version=2,
                request_id=f"{args.run_id}:bid-b",
            ),
            pb2.BidRequest(
                auction_id=created.auction_id,
                bidder_id="bidder-a",
                amount=180,
                expected_version=3,
                request_id=f"{args.run_id}:replace-a",
            ),
        ]
        for request in mutations:
            response = stub.PlaceBid(request, timeout=15)
            require(response.success, "prepare-state", response.message)
        withdrawn = stub.WithdrawBid(
            pb2.WithdrawBidRequest(
                auction_id=created.auction_id,
                bidder_id="bidder-b",
                expected_version=4,
                request_id=f"{args.run_id}:withdraw-b",
            ),
            timeout=15,
        )
        require(
            withdrawn.success and withdrawn.final_version == 5,
            "prepare-state",
            withdrawn.message,
        )
        print("  READY → nontrivial version 5 committed")

        protected_services = [
            primary[0]["metric"]["instance"].split(":")[0],
            backup[0]["metric"]["instance"].split(":")[0],
        ]
        before = [snapshot(args.compose_file, service, created.auction_id) for service in protected_services]
        require(before[0] == before[1], "pre-restart-replication", "protected replicas differ")

        compose(args.compose_file, "restart", *services)
        print("  storage services restarted with persistent volumes")
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            ready = query(
                args.prometheus_url,
                'blindsided_cluster_ready{job="controller"} == 1',
            )
            ready_backups = query(
                args.prometheus_url,
                '(blindsided_storage_role{job="storage",role="backup"} == 1) '
                'and on(instance) (blindsided_storage_ready{job="storage"} == 1)',
            )
            if len(ready) == 1 and len(ready_backups) == 1:
                break
            time.sleep(2)
        else:
            raise RuntimeError(
                "transition=restart-recovery: no synchronized backup before deadline"
            )

        replay = stub.PlaceBid(mutations[-1], timeout=15)
        require(replay.success, "idempotency-after-restart", replay.message)
        reveal = stub.RevealAuction(
            pb2.RevealAuctionRequest(
                auction_id=created.auction_id,
                seller_id=seller,
                expected_version=5,
                request_id=f"{args.run_id}:reveal",
            ),
            timeout=15,
        )
        require(
            reveal.ok and reveal.final_version == 6,
            "reveal-after-restart",
            f"version={reveal.final_version}: {reveal.message}",
        )
        final = stub.GetAuction(
            pb2.GetAuctionRequest(auction_id=created.auction_id), timeout=15
        )
        require(
            final.ok
            and final.auction.result.winning_bidder_id == "bidder-a"
            and abs(final.auction.result.winning_amount - 180) < 0.001,
            "deterministic-outcome",
            "winner or amount changed across restart",
        )
        current_primary = query(
            args.prometheus_url,
            'blindsided_storage_role{job="storage",role="primary"} == 1',
        )[0]["metric"]["instance"].split(":")[0]
        current_backup = query(
            args.prometheus_url,
            '(blindsided_storage_role{job="storage",role="backup"} == 1) '
            'and on(instance) (blindsided_storage_ready{job="storage"} == 1)',
        )[0]["metric"]["instance"].split(":")[0]
        after = [
            snapshot(args.compose_file, service, created.auction_id)
            for service in (current_primary, current_backup)
        ]
        require(after[0] == after[1], "final-replication", "final snapshots differ")
        require(after[0].version == 6, "final-version", f"got {after[0].version}")
        require(after[0].ends_at == before[0].ends_at, "deadline", "ends_at changed")
        require(
            after[0].bids["bidder-a"].acceptance_order
            == before[0].bids["bidder-a"].acceptance_order,
            "acceptance-order",
            "acceptance order changed",
        )
        print("  READY → version 6 revealed identically; deadline/order/idempotency survived")

    print(
        "Metrics: verify restart health transitions, readiness/role/epoch, "
        "idempotency replay, replication, commits, and RPC latency."
    )


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, subprocess.CalledProcessError, grpc.RpcError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
