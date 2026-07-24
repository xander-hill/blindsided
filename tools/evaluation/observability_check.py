#!/usr/bin/env python3
"""Generate demo activity and validate only dashboard-backed metric signals."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]

# Counters/histogram counts must move during this invocation. Direct before/after
# deltas also handle a label set which is created for the first time (Prometheus
# increase() cannot infer the missing zero-valued sample in that case).
DELTA_QUERIES = {
    "RPC outcomes": (
        "sum by (instance,service,method,result) (blindsided_rpc_requests_total)",
    ),
    "RPC latency": (
        "sum by (instance,service,method,result) (blindsided_rpc_duration_seconds_count)",
    ),
    "mutation outcomes": (
        "sum by (instance,operation,outcome) (blindsided_mutations_total)",
    ),
    "concurrency retries/conflicts": (
        "sum by (instance,operation,outcome) (blindsided_concurrency_retries_total)",
    ),
    "replication prepare/commit": (
        "sum by (instance,operation,outcome) (blindsided_replication_attempts_total)",
        "sum by (instance,operation,outcome) (blindsided_commits_total)",
    ),
    "synchronization attempts/outcomes": (
        "sum by (instance,outcome) (blindsided_synchronization_attempts_total)",
    ),
    "failover/recovery": (
        "sum by (instance,outcome) (blindsided_failovers_total)",
        "sum by (instance,outcome) (blindsided_promotion_attempts_total)",
        "sum by (instance,transition) (blindsided_replica_health_transitions_total)",
    ),
    "watch streams and updates": (
        "sum by (instance,outcome) (blindsided_watch_streams_total)",
        "sum by (instance) (blindsided_watch_updates_total)",
    ),
}

GAUGE_QUERIES = {
    "readiness": 'blindsided_cluster_ready{job="controller"} == 1',
    "registered replicas": 'blindsided_registered_replicas{job="controller"} == 3',
    "healthy replicas": 'blindsided_healthy_replicas{job="controller"} == 3',
    "one ready primary": (
        'count((blindsided_storage_role{job="storage",role="primary"} == 1) '
        'and on(instance) (blindsided_storage_ready{job="storage"} == 1)) == 1'
    ),
    "one synchronized backup": (
        'count((blindsided_storage_role{job="storage",role="backup"} == 1) '
        'and on(instance) (blindsided_storage_ready{job="storage"} == 1)) == 1'
    ),
    "storage roles": 'count(blindsided_storage_role{job="storage"}) >= 3',
    "epoch": 'blindsided_primary_epoch{job="controller"} > 0',
    "watch streams closed": "sum(blindsided_active_watch_streams) == bool 0",
}


def query(base_url: str, promql: str) -> list[dict]:
    url = f"{base_url}/api/v1/query?{urllib.parse.urlencode({'query': promql})}"
    with urllib.request.urlopen(url, timeout=5) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query failed: {promql}: {payload}")
    return payload["data"]["result"]


def positive(results: list[dict]) -> bool:
    return bool(results) and any(float(item["value"][1]) > 0 for item in results)


def total(results: list[dict]) -> float:
    return sum(float(item["value"][1]) for item in results)


def series_values(results: list[dict]) -> dict[tuple[tuple[str, str], ...], float]:
    return {
        tuple(sorted(item["metric"].items())): float(item["value"][1])
        for item in results
    }


def counter_moved(before: dict, after_results: list[dict]) -> bool:
    after = series_values(after_results)
    return any(value > before.get(labels, 0) for labels, value in after.items())


def run_activity(args: argparse.Namespace) -> None:
    common = ["--address", args.address, "--prometheus-url", args.prometheus_url]
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/evaluation/concurrent_bidding.py"),
            "--bidders",
            "8",
            "--run-id",
            f"observability-concurrency-{args.run_id}",
            *common,
        ],
        cwd=ROOT,
        check=True,
    )
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "tools/evaluation/watch_behavior.py"),
            "--run-id",
            f"observability-watch-{args.run_id}",
            "--timeout",
            str(args.timeout),
            *common,
        ],
        cwd=ROOT,
        check=True,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prometheus-url", default="http://localhost:9090")
    parser.add_argument("--address", default="localhost:50052")
    parser.add_argument("--timeout", type=float, default=90)
    parser.add_argument("--run-id", default=str(uuid4()))
    parser.add_argument("--skip-activity", action="store_true")
    args = parser.parse_args()

    dashboards = sorted((ROOT / "deploy/grafana/dashboards").glob("*.json"))
    if not dashboards:
        raise RuntimeError("transition=grafana-provisioning: no dashboards found")
    for dashboard in dashboards:
        with dashboard.open(encoding="utf-8") as source:
            json.load(source)
    print(f"Timeline:\n  Grafana → {len(dashboards)} dashboard definitions valid")

    started_at = time.time()
    baselines = {
        expression: series_values(query(args.prometheus_url, expression))
        for expressions in DELTA_QUERIES.values()
        for expression in expressions
    }
    if not args.skip_activity:
        run_activity(args)
        print("  activity → concurrency, replication, watch, failover, and recovery generated")

    # Include a scrape on both sides of every event, plus a small clock margin.
    window = max(60, int(math.ceil(time.time() - started_at)) + 30)
    deadline = time.monotonic() + 30
    failures = []
    while True:
        failures = []
        for signal, templates in DELTA_QUERIES.items():
            for template in templates:
                expression = template
                results = query(args.prometheus_url, expression)
                if not counter_moved(baselines[expression], results):
                    failures.append(
                        {
                            "kind": "missing instrumentation",
                            "signal": signal,
                            "query": expression,
                            "before_series": [
                                {"labels": dict(labels), "value": value}
                                for labels, value in baselines[expression].items()
                            ],
                            "series": [
                                {
                                    "labels": item["metric"],
                                    "value": item["value"][1],
                                }
                                for item in results
                            ],
                        }
                    )
        if not failures or time.monotonic() >= deadline:
            break
        time.sleep(2)

    for signal, expression in GAUGE_QUERIES.items():
        results = query(args.prometheus_url, expression)
        if not positive(results):
            failures.append(
                {
                    "kind": "failed system behavior/current state",
                    "signal": signal,
                    "query": expression,
                    "series": [
                        {"labels": item["metric"], "value": item["value"][1]}
                        for item in results
                    ],
                }
            )

    if failures:
        raise RuntimeError(
            "transition=observability-validation:\n"
            + json.dumps(failures, indent=2, sort_keys=True)
        )
    for signal in (*DELTA_QUERIES, *GAUGE_QUERIES):
        print(f"  Prometheus → {signal}")
    print(f"All dashboard/demo signals passed using fresh counter deltas over {window}s.")


if __name__ == "__main__":
    try:
        main()
    except (
        RuntimeError,
        OSError,
        ValueError,
        subprocess.CalledProcessError,
    ) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
