#!/usr/bin/env python3
"""Verify that final-demo metric families and provisioned dashboards are populated."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[2]

QUERIES = {
    "RPC outcomes/latency": (
        "sum(blindsided_rpc_requests_total) > 0",
        "count(blindsided_rpc_duration_seconds_count) > 0",
    ),
    "mutation outcomes": ("sum(blindsided_mutations_total) > 0",),
    "concurrency retries/conflicts": (
        "sum(blindsided_concurrency_retries_total) > 0",
    ),
    "replication prepare/commit": (
        "sum(blindsided_replication_attempts_total) > 0",
        "sum(blindsided_commits_total) > 0",
    ),
    "synchronization/recovery": (
        "count(blindsided_synchronization_attempts_total) > 0",
        "count(blindsided_failovers_total) > 0",
    ),
    "health/readiness/roles/epoch": (
        "count(blindsided_cluster_ready) > 0",
        "count(blindsided_storage_ready) == 3",
        "count(blindsided_storage_role) >= 3",
        "count(blindsided_primary_epoch) > 0",
    ),
    "watch streams/updates": (
        "count(blindsided_active_watch_streams) > 0",
        "sum(blindsided_watch_updates_total) > 0",
    ),
}


def query(base_url: str, promql: str) -> bool:
    url = f"{base_url}/api/v1/query?{urllib.parse.urlencode({'query': promql})}"
    with urllib.request.urlopen(url, timeout=5) as response:
        result = json.load(response)["data"]["result"]
    return bool(result) and any(float(item["value"][1]) != 0 for item in result)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    args = parser.parse_args()

    dashboards = sorted((ROOT / "deploy/grafana/dashboards").glob("*.json"))
    if not dashboards:
        raise RuntimeError("transition=grafana-provisioning: no dashboards found")
    for dashboard in dashboards:
        with dashboard.open(encoding="utf-8") as source:
            json.load(source)
    print(f"Timeline:\n  Grafana → {len(dashboards)} dashboard definitions valid")

    for signal, expressions in QUERIES.items():
        for expression in expressions:
            if not query(args.prometheus_url, expression):
                raise RuntimeError(
                    f"transition=metric-signal:{signal}: no series/value for {expression}"
                )
        print(f"  Prometheus → {signal}")
    print("Metrics are supporting signals only; scenario state assertions are authoritative.")


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError, ValueError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
