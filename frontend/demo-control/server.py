#!/usr/bin/env python3
"""Local-only, fixed-action adapter for the Blindsided portfolio demo."""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_FILE = ROOT / "deploy" / "compose" / "docker-compose.yaml"
PROMETHEUS_URL = os.environ.get("PROMETHEUS_URL", "http://127.0.0.1:9090").rstrip("/")
HOST = os.environ.get("DEMO_CONTROL_HOST", "127.0.0.1")
PORT = int(os.environ.get("DEMO_CONTROL_PORT", "8090"))
STORAGE_SERVICES = ("storage-0", "storage-1", "storage-2")
ACTION_LOCK = threading.Lock()
EVENTS: deque[dict[str, Any]] = deque(maxlen=50)
LAST_FAILED: dict[str, str] = {}
LAST_STATUS: dict[str, Any] | None = None


def query(expression: str) -> list[dict[str, Any]]:
    url = f"{PROMETHEUS_URL}/api/v1/query?{urllib.parse.urlencode({'query': expression})}"
    with urllib.request.urlopen(url, timeout=2) as response:
        payload = json.load(response)
    if payload.get("status") != "success":
        raise RuntimeError("Prometheus query failed")
    return payload["data"]["result"]


def scalar(expression: str) -> float | None:
    results = query(expression)
    if len(results) != 1:
        return None
    try:
        return float(results[0]["value"][1])
    except (KeyError, TypeError, ValueError):
        return None


def replicas_for(role: str, ready: bool | None = None) -> list[dict[str, Any]]:
    expression = (
        f'(blindsided_storage_role{{job="storage",role="{role}"}} == 1)'
        ' and on(instance) (up{job="storage"} == 1)'
    )
    if ready is not None:
        expected = 1 if ready else 0
        expression += (
            f' and on(instance) (blindsided_storage_ready{{job="storage"}} == {expected})'
        )
    replicas = []
    for item in query(expression):
        instance = item.get("metric", {}).get("instance", "")
        service = instance.split(":", 1)[0]
        if service not in STORAGE_SERVICES:
            continue
        epoch = scalar(f'blindsided_storage_epoch{{job="storage",instance="{instance}"}}')
        replicas.append({
            "id": service,
            "role": "primary" if role == "primary" else (
                "synchronous-backup" if ready else "standby"
            ),
            "healthy": True,
            "ready": role == "primary" or bool(ready),
            "epoch": int(epoch) if epoch is not None else None,
        })
    return replicas


def summary_metrics() -> dict[str, Any]:
    def rounded(value: float | None, digits: int = 0):
        return None if value is None else round(value, digits)
    return {
        "failoversCompleted": rounded(scalar(
            'sum(blindsided_failovers_total{outcome="completed"}) or vector(0)'
        )),
        "reprotectionsCompleted": rounded(scalar(
            'sum(blindsided_synchronization_attempts_total{outcome="completed"}) or vector(0)'
        )),
        # Prometheus histograms expose aggregates, not the last observation.
        # Keep this unknown instead of mislabelling the lifetime mean as "last".
        "lastFailoverSeconds": None,
        "mutationSuccesses": rounded(scalar(
            'sum(blindsided_mutations_total{outcome=~"success|committed"}) or vector(0)'
        )),
        "mutationFailures": rounded(scalar(
            'sum(blindsided_mutations_total{outcome!~"success|committed"}) or vector(0)'
        )),
        "serviceReplicas": rounded(scalar('sum(up{job="auction-service"} == 1)')),
    }


def add_event(title: str, category: str, severity: str = "info", detail: str | None = None):
    EVENTS.appendleft({
        "id": f"system-{time.time_ns()}",
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "category": category,
        "title": title,
        "detail": detail,
        "severity": severity,
    })


def cluster_status() -> dict[str, Any]:
    global LAST_STATUS
    primary = replicas_for("primary")
    backup = replicas_for("backup", True)
    standbys = replicas_for("backup", False)
    ready = scalar('blindsided_cluster_ready{job="controller"}') == 1
    epoch = scalar('blindsided_primary_epoch{job="controller"}')
    protected = bool(primary and backup and ready)
    writes_available = bool(primary and ready)
    healthy = scalar('blindsided_healthy_replicas{job="controller"}') or 0
    if protected:
        state = "READY"
    elif primary and writes_available:
        state = "REPROTECTING"
    elif healthy > 0:
        state = "FAILING_OVER"
    else:
        state = "UNAVAILABLE"
    status = {
        "state": state,
        "epoch": int(epoch) if epoch is not None else None,
        "primary": primary[0] if len(primary) == 1 else None,
        "synchronousBackup": backup[0] if len(backup) == 1 else None,
        "standbys": standbys,
        "protected": protected,
        "writesAvailable": writes_available,
        "activeWatchStreams": scalar(
            'sum(blindsided_active_watch_streams{job="auction-service"}) or vector(0)'
        ),
        "metrics": summary_metrics(),
        "observedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if LAST_STATUS:
        if status["epoch"] != LAST_STATUS["epoch"]:
            add_event("Epoch advanced", "failover", "success",
                      f'{LAST_STATUS["epoch"]} → {status["epoch"]}')
        if status["state"] != LAST_STATUS["state"]:
            labels = {
                "READY": ("Cluster protected; writes resumed", "success"),
                "REPROTECTING": ("Reprotection in progress", "warning"),
                "FAILING_OVER": ("Primary promotion in progress", "warning"),
                "UNAVAILABLE": ("No write-ready primary", "critical"),
            }
            title, severity = labels[state]
            add_event(title, "system", severity)
    LAST_STATUS = status
    return status


def compose(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ("docker", "compose", "-f", str(COMPOSE_FILE), *args),
        cwd=ROOT, check=True, capture_output=True, text=True, timeout=90,
    )


def service_for_role(role: str) -> str:
    candidates = replicas_for(role, True if role == "backup" else None)
    if len(candidates) != 1:
        raise RuntimeError(f"Cannot identify exactly one healthy {role}")
    return candidates[0]["id"]


def perform_action(action: str) -> str:
    if action == "fail-backup":
        service = service_for_role("backup")
        compose("stop", service)
        LAST_FAILED["backup"] = service
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            replacements = replicas_for("backup", True)
            if len(replacements) == 1 and replacements[0]["id"] != service:
                compose("start", service)
                LAST_FAILED.pop("backup", None)
                return f"Backup {service} failed; replacement synchronized and replica restored"
            time.sleep(2)
        # Match the evaluation script's cleanup guarantee even on timeout.
        compose("start", service)
        LAST_FAILED.pop("backup", None)
        raise RuntimeError("Replacement backup did not synchronize within 90 seconds; failed replica was restored")
    if action == "fail-primary":
        service = service_for_role("primary")
        compose("stop", service)
        LAST_FAILED["primary"] = service
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            promoted = replicas_for("primary")
            replacement = replicas_for("backup", True)
            if (len(promoted) == 1 and promoted[0]["id"] != service
                    and len(replacement) == 1):
                compose("start", service)
                LAST_FAILED.pop("primary", None)
                return f"Primary {service} failed; promotion completed and replica restored"
            time.sleep(2)
        compose("start", service)
        LAST_FAILED.pop("primary", None)
        raise RuntimeError("Primary promotion did not complete within 90 seconds; failed replica was restored")
    if action in {"restart-backup", "restart-primary"}:
        role = action.removeprefix("restart-")
        service = LAST_FAILED.get(role)
        if not service:
            raise RuntimeError(f"No {role} failed through this adapter")
        compose("start", service)
        LAST_FAILED.pop(role, None)
        return f"{service} restarted"
    if action == "restart-cluster":
        compose("restart", "controller", *STORAGE_SERVICES, "service-node", "envoy")
        LAST_FAILED.clear()
        return "Cluster services restarted"
    raise RuntimeError("Unknown action")


class Handler(BaseHTTPRequestHandler):
    def cors(self):
        origin = self.headers.get("Origin", "")
        allowed = {"http://localhost:5173", "http://127.0.0.1:5173"}
        if origin in allowed:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def send_json(self, status: int, payload: dict[str, Any]):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.cors()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_response(204)
        self.cors()
        self.end_headers()

    def do_GET(self):
        try:
            if self.path == "/demo/status":
                self.send_json(200, cluster_status())
            elif self.path == "/demo/events":
                self.send_json(200, {"events": list(EVENTS)})
            elif self.path == "/demo/metrics":
                self.send_json(200, summary_metrics())
            else:
                self.send_json(404, {"error": "Not found"})
        except Exception as exc:
            self.send_json(503, {"error": str(exc)})

    def do_POST(self):
        prefix = "/demo/actions/"
        if not self.path.startswith(prefix):
            self.send_json(404, {"error": "Not found"})
            return
        if not ACTION_LOCK.acquire(blocking=False):
            self.send_json(409, {"error": "Another destructive action is running"})
            return
        action = self.path.removeprefix(prefix)
        try:
            message = perform_action(action)
            add_event(message, "failover" if "primary" in action else "replication",
                      "warning" if action.startswith("fail") else "info")
            self.send_json(200, {"ok": True, "message": message})
        except Exception as exc:
            self.send_json(400, {"error": str(exc)})
        finally:
            ACTION_LOCK.release()

    def log_message(self, format: str, *args: Any):
        print(f"[demo-control] {format % args}")


if __name__ == "__main__":
    print(f"Demo control listening on http://{HOST}:{PORT} (local use only)")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
