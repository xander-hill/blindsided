import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch


SERVER_PATH = Path(__file__).with_name("server.py")
SPEC = importlib.util.spec_from_file_location("demo_control_server", SERVER_PATH)
assert SPEC and SPEC.loader
server = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(server)


class DemoControlTests(unittest.TestCase):
    def test_restart_primary_resolves_current_primary_and_restarts_it(self):
        with (
            patch.object(server, "service_for_role", return_value="storage-2") as resolve,
            patch.object(server, "compose") as compose,
        ):
            message = server.perform_action("restart-primary")

        resolve.assert_called_once_with("primary")
        compose.assert_called_once_with("restart", "storage-2")
        self.assertEqual(message, "Current primary storage-2 restarted")

    def test_restart_primary_surfaces_resolution_error(self):
        with patch.object(
            server,
            "service_for_role",
            side_effect=RuntimeError("Cannot identify exactly one healthy primary"),
        ):
            with self.assertRaisesRegex(RuntimeError, "exactly one healthy primary"):
                server.perform_action("restart-primary")

    def test_summary_metrics_uses_completed_failover_total(self):
        expressions = []

        def fake_scalar(expression):
            expressions.append(expression)
            return 4

        with patch.object(server, "scalar", side_effect=fake_scalar):
            metrics = server.summary_metrics()

        self.assertEqual(metrics["failoversCompleted"], 4)
        self.assertIn(
            'sum(blindsided_failovers_total{outcome="completed"}) or vector(0)',
            expressions,
        )
        self.assertIsNone(metrics["lastFailoverSeconds"])

    def test_scalar_rejects_ambiguous_series(self):
        with patch.object(server, "query", return_value=[
            {"value": [0, "1"]},
            {"value": [0, "2"]},
        ]):
            self.assertIsNone(server.scalar("unaggregated_metric"))


if __name__ == "__main__":
    unittest.main()
