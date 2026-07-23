from contextlib import redirect_stdout
import importlib.util
import io
from pathlib import Path

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
