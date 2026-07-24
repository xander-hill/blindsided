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
from google.protobuf.json_format import MessageToDict
from google.protobuf import timestamp_pb2

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
from blindsided.generated import blindsided_pb2 as pb2  # noqa: E402
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc  # noqa: E402


READY_PRIMARY_QUERY = (
    '(blindsided_storage_role{job="storage",role="primary"} == 1) '
    'and on(instance) (blindsided_storage_ready{job="storage"} == 1)'
)
READY_BACKUP_QUERY = (
    '(blindsided_storage_role{job="storage",role="backup"} == 1) '
    'and on(instance) (blindsided_storage_ready{job="storage"} == 1)'
)
CLUSTER_READY_QUERY = 'blindsided_cluster_ready{job="controller"} == 1'


def require(condition: bool, transition: str, detail: str) -> None:
    if not condition:
        raise RuntimeError(f"transition={transition}: {detail}")


def query(prometheus: str, promql: str) -> list[dict]:
    url = f"{prometheus}/api/v1/query?{urllib.parse.urlencode({'query': promql})}"
    with urllib.request.urlopen(url, timeout=5) as response:
        return json.load(response)["data"]["result"]


def _fresh(results: list[dict], fresh_after: float | None) -> list[dict]:
    if fresh_after is None:
        return results
    return [
        result
        for result in results
        if float(result["value"][0]) >= fresh_after
    ]


def wait_for_protected_topology(
    prometheus: str,
    timeout: float,
    *,
    fresh_after: float | None = None,
    stable_samples: int = 3,
) -> tuple[list[dict], list[dict], list[dict]]:
    """Wait until the same protected topology is observed repeatedly."""
    deadline = time.monotonic() + timeout
    previous_signature = None
    consecutive_samples = 0
    latest: dict[str, list[dict]] = {
        "ready_primary": [],
        "ready_backup": [],
        "cluster_ready": [],
    }
    while True:
        latest = {
            "ready_primary": query(prometheus, READY_PRIMARY_QUERY),
            "ready_backup": query(prometheus, READY_BACKUP_QUERY),
            "cluster_ready": query(prometheus, CLUSTER_READY_QUERY),
        }
        primary = _fresh(latest["ready_primary"], fresh_after)
        backup = _fresh(latest["ready_backup"], fresh_after)
        cluster = _fresh(latest["cluster_ready"], fresh_after)
        if len(primary) == len(backup) == len(cluster) == 1:
            signature = (
                primary[0]["metric"].get("instance"),
                backup[0]["metric"].get("instance"),
            )
            if signature == previous_signature:
                consecutive_samples += 1
            else:
                previous_signature = signature
                consecutive_samples = 1
            if consecutive_samples >= stable_samples:
                return primary, backup, cluster
        else:
            previous_signature = None
            consecutive_samples = 0
        if time.monotonic() >= deadline:
            print(
                "Protected topology timeout metrics:\n"
                + json.dumps(latest, indent=2, sort_keys=True),
                file=sys.stderr,
            )
            raise RuntimeError(
                "transition=ready-protected-topology: expected exactly one "
                "fresh ready primary, one synchronized ready backup, and "
                "cluster_ready == 1"
            )
        time.sleep(2)


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


def snapshot(compose_file: str, service: str) -> pb2.StorageSnapshot:
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
            "print(base64.b64encode(s.SerializeToString(deterministic=True)).decode())"
        ),
        capture=True,
    )
    state = pb2.StorageSnapshot()
    state.ParseFromString(base64.b64decode(encoded))
    return state


def auction_from(state: pb2.StorageSnapshot, auction_id: str) -> pb2.Auction:
    matches = [auction for auction in state.auctions if auction.auction_id == auction_id]
    require(
        len(matches) == 1,
        "snapshot-auction",
        f"expected auction {auction_id!r} exactly once, found {len(matches)}",
    )
    return matches[0]


def _message_dict(message) -> dict:
    return MessageToDict(
        message,
        preserving_proto_field_name=True,
        use_integers_for_enums=False,
    )


def normalized_replicated_state(state: pb2.StorageSnapshot) -> dict:
    """Return all transactional fields which must be identical on a protected pair."""
    collections = {
        "auctions": {
            item.auction_id: _message_dict(item) for item in state.auctions
        },
        "idempotency_records": {
            item.request_id: _message_dict(item)
            for item in state.idempotency_records
        },
        "prepared_mutations": {
            item.request_id: _message_dict(item)
            for item in state.prepared_mutations
        },
        "aborted_mutations": {
            item.request_id: _message_dict(item)
            for item in state.aborted_mutations
        },
        "pending_backup_commits": {
            item.request_id: _message_dict(item)
            for item in state.pending_backup_commits
        },
    }
    return collections


def replica_metadata(state: pb2.StorageSnapshot) -> dict:
    return {
        "current_epoch": state.current_epoch,
        "promotion_ready": state.promotion_ready,
        "synchronous_backup_address": state.synchronous_backup_address,
    }


def field_diff(left, right, path: str = "$") -> list[dict]:
    if isinstance(left, dict) and isinstance(right, dict):
        differences = []
        for key in sorted(set(left) | set(right)):
            child = f"{path}.{key}"
            if key not in left:
                differences.append({"path": child, "primary": "<missing>", "backup": right[key]})
            elif key not in right:
                differences.append({"path": child, "primary": left[key], "backup": "<missing>"})
            else:
                differences.extend(field_diff(left[key], right[key], child))
        return differences
    if isinstance(left, list) and isinstance(right, list):
        differences = []
        for index in range(max(len(left), len(right))):
            child = f"{path}[{index}]"
            if index >= len(left):
                differences.append({"path": child, "primary": "<missing>", "backup": right[index]})
            elif index >= len(right):
                differences.append({"path": child, "primary": left[index], "backup": "<missing>"})
            else:
                differences.extend(field_diff(left[index], right[index], child))
        return differences
    if left != right:
        return [{"path": path, "primary": left, "backup": right}]
    return []


def compare_protected_pair(
    transition: str,
    primary_state: pb2.StorageSnapshot,
    backup_state: pb2.StorageSnapshot,
    auction_id: str,
) -> tuple[pb2.Auction, pb2.Auction]:
    primary_auction = auction_from(primary_state, auction_id)
    backup_auction = auction_from(backup_state, auction_id)
    auction_diff = field_diff(
        _message_dict(primary_auction), _message_dict(backup_auction)
    )
    durable_diff = field_diff(
        normalized_replicated_state(primary_state),
        normalized_replicated_state(backup_state),
    )
    report = {
        "transition": transition,
        "auction_id": auction_id,
        "scenario_auction_diff": auction_diff,
        "complete_replicated_state_diff": durable_diff,
        "primary_replica_metadata": replica_metadata(primary_state),
        "backup_replica_metadata": replica_metadata(backup_state),
    }
    print("  Replica comparison:\n" + json.dumps(report, indent=2, sort_keys=True))
    require(
        not auction_diff,
        transition,
        "scenario auction differs; normalized diff printed above",
    )
    require(
        not durable_diff,
        transition,
        "complete replicated state differs; normalized diff printed above",
    )
    return primary_auction, backup_auction


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
    primary, backup, _ = wait_for_protected_topology(
        args.prometheus_url,
        args.timeout,
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
        before_states = [
            snapshot(args.compose_file, service) for service in protected_services
        ]
        before = compare_protected_pair(
            "pre-restart-replication",
            before_states[0],
            before_states[1],
            created.auction_id,
        )

        compose(args.compose_file, "restart", *services)
        restarted_at = time.time()
        print("  storage services restarted with persistent volumes")
        recovered_primary, recovered_backup, _ = wait_for_protected_topology(
            args.prometheus_url,
            args.timeout,
            fresh_after=restarted_at,
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
        # Simultaneous replica restarts can briefly expose the previously
        # designated backup before the controller finishes re-protection.
        # Resolve the stable protected pair after replay/reveal activity rather
        # than comparing a cached, transient backup as if it were authoritative.
        current_primary_metrics, current_backup_metrics, _ = (
            wait_for_protected_topology(
                args.prometheus_url,
                args.timeout,
                fresh_after=restarted_at,
            )
        )
        current_primary = current_primary_metrics[0]["metric"]["instance"].split(":")[0]
        current_backup = current_backup_metrics[0]["metric"]["instance"].split(":")[0]
        after_states = [
            snapshot(args.compose_file, service)
            for service in (current_primary, current_backup)
        ]
        after = compare_protected_pair(
            "final-replication",
            after_states[0],
            after_states[1],
            created.auction_id,
        )
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
