from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path
from unittest import mock

from blindsided.generated import blindsided_pb2 as pb2
from backend.tests.helpers import BackendTestCase


def load_auction_traffic():
    path = (
        Path(__file__).resolve().parents[3]
        / "tools"
        / "evaluation"
        / "auction_traffic.py"
    )
    spec = importlib.util.spec_from_file_location("evaluation_auction_traffic", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_restart_durability():
    path = (
        Path(__file__).resolve().parents[3]
        / "tools"
        / "evaluation"
        / "restart_durability.py"
    )
    spec = importlib.util.spec_from_file_location(
        "evaluation_restart_durability", path
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EvaluationScriptTests(BackendTestCase):
    def test_create_success_is_announced_only_after_commit_confirmation(self):
        script = load_auction_traffic()
        output = io.StringIO()
        response = pb2.CreateAuctionResponse(ok=True, auction_id="auction-1")

        with redirect_stdout(output):
            auction_id = script.confirm_created(response)

        self.assertEqual(auction_id, "auction-1")
        self.assertIn("[ok] CreateAuction", output.getvalue())

    def test_uncommitted_create_prints_no_success_marker(self):
        script = load_auction_traffic()
        output = io.StringIO()
        response = pb2.CreateAuctionResponse(
            ok=False,
            message="Synchronous backup preparation failed.",
        )

        with (
            redirect_stdout(output),
            self.assertRaisesRegex(RuntimeError, "was not committed"),
        ):
            script.confirm_created(response)

        self.assertNotIn("[ok] CreateAuction", output.getvalue())

    def test_restart_topology_wait_ignores_empty_and_stale_results(self):
        script = load_restart_durability()
        stale = [{"metric": {"instance": "old:8000"}, "value": [99, "1"]}]
        fresh = [{"metric": {"instance": "new:8000"}, "value": [101, "1"]}]
        responses = [
            [], stale, stale,
            fresh, fresh, fresh,
            fresh, fresh, fresh,
            fresh, fresh, fresh,
        ]

        with (
            mock.patch.object(script, "query", side_effect=responses),
            mock.patch.object(script.time, "monotonic", return_value=0),
            mock.patch.object(script.time, "sleep"),
        ):
            primary, backup, cluster = script.wait_for_protected_topology(
                "http://prometheus", 5, fresh_after=100
            )

        self.assertEqual(primary, fresh)
        self.assertEqual(backup, fresh)
        self.assertEqual(cluster, fresh)

    def test_restart_topology_timeout_prints_current_metrics(self):
        script = load_restart_durability()
        stderr = io.StringIO()

        with (
            redirect_stdout(io.StringIO()),
            mock.patch("sys.stderr", stderr),
            mock.patch.object(script, "query", return_value=[]),
            mock.patch.object(script.time, "monotonic", side_effect=[0, 1]),
            self.assertRaisesRegex(RuntimeError, "ready-protected-topology"),
        ):
            script.wait_for_protected_topology(
                "http://prometheus", 1, fresh_after=100
            )

        self.assertIn("Protected topology timeout metrics", stderr.getvalue())
        self.assertIn('"ready_primary": []', stderr.getvalue())
