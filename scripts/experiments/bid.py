import sys
from pathlib import Path

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc

def place_bid(amount, buyer_id):
    # Connect through Envoy port-forward
    with grpc.insecure_channel('localhost:8080') as channel:
        stub = pb2_grpc.BlindSidedStub(channel)
        
        print(f"🚀 Sending bid for ${amount} from {buyer_id}...")
        
        # PlaceSecretBid expects auction_id, buyer_id, amount, and expected_version
        # Note: Set expected_version to 0 to bypass strict OCC check for this test
        request = pb2.BidRequest(
            auction_id="vintage-rolex",
            buyer_id=buyer_id,
            amount=amount,
            expected_version=0 
        )
        
        resp = stub.PlaceSecretBid(request)
        print(f"✅ Result: {resp.message}")

if __name__ == "__main__":
    # Change these values to "up" the bid!
    place_bid(amount=11500.0, buyer_id="speedmaster_fan_99")
