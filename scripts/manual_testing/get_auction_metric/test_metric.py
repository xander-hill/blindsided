#!/usr/bin/env python3
"""Manually verify successful GetAuction Prometheus tracking."""

import os
import re
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen
from uuid import uuid4

import grpc
from google.protobuf import timestamp_pb2

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc


SERVICE_ADDRESS = os.getenv("SERVICE_ADDRESS", "localhost:50052")
METRICS_URL = os.getenv("METRICS_URL", "http://localhost:8000/metrics")
METRIC_PATTERN = re.compile(
    r'^blindsided_rpc_requests_total\{'
    r'(?=[^}]*method="GetAuction")'
    r'(?=[^}]*result="success")'
    r'(?=[^}]*service="AuctionService")[^}]*\}\s+([0-9.eE+-]+)$',
    re.MULTILINE,
)


def successful_get_count() -> float:
    try:
        with urlopen(METRICS_URL, timeout=3) as response:
            payload = response.read().decode("utf-8")
    except URLError as error:
        raise RuntimeError(f"metrics endpoint is unavailable at {METRICS_URL}") from error

    match = METRIC_PATTERN.search(payload)
    return float(match.group(1)) if match else 0.0


def create_auction(stub: pb2_grpc.AuctionServiceStub) -> str:
    ends_at = timestamp_pb2.Timestamp()
    ends_at.FromSeconds(int(time.time()) + 3600)
    request_id = f"manual-metric-{uuid4()}"
    response = stub.CreateAuction(
        pb2.CreateAuctionRequest(
            request_id=request_id,
            seller_id="manual-metric-seller",
            title="Manual metric test auction",
            category="testing",
            description="Created by the GetAuction metric smoke test",
            reserve_price=1.0,
            ends_at=ends_at,
        ),
        timeout=10,
    )
    if not response.ok:
        raise RuntimeError(f"CreateAuction failed: {response.message}")
    return response.auction_id


def main() -> int:
    try:
        with grpc.insecure_channel(SERVICE_ADDRESS) as channel:
            grpc.channel_ready_future(channel).result(timeout=5)
            stub = pb2_grpc.AuctionServiceStub(channel)
            auction_id = create_auction(stub)
            before = successful_get_count()
            response = stub.GetAuction(
                pb2.GetAuctionRequest(auction_id=auction_id),
                timeout=5,
            )
            after = successful_get_count()

        if not response.ok:
            raise RuntimeError(f"GetAuction failed: {response.message}")

        print(f"auction_id: {auction_id}")
        print(f"counter before: {before:g}")
        print(f"counter after:  {after:g}")
        if after != before + 1:
            raise RuntimeError(
                f"expected counter to increase by 1, but it changed by {after - before:g}"
            )
        print("PASS: successful GetAuction incremented the metric by 1")
        return 0
    except (grpc.RpcError, RuntimeError, TimeoutError) as error:
        print(f"FAIL: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
