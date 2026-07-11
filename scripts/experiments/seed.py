# Save as seed_auction.py
import sys
from pathlib import Path

import grpc

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from blindsided.generated import blindsided_pb2 as pb2
from blindsided.generated import blindsided_pb2_grpc as pb2_grpc

def run():
    # We hit Envoy, which we know works based on your previous logs
    with grpc.insecure_channel('localhost:8080') as channel:
        stub = pb2_grpc.BlindSidedStub(channel)
        
        # Create the Rolex auction
        auction = pb2.Auction(
            auction_id="vintage-rolex",
            title="Vintage Rolex Submariner",
            description="Reference 5513, ghost bezel, original tritium dial.",
            reserve_price=12000.0,
            bids={"house_bidder": 8500.0}, # Initial bid to trigger the range logic
            version=1
        )
        
        print("🌱 Seeding auction 'vintage-rolex'...")
        resp = stub.OpenAuction(pb2.OpenRequest(auction=auction))
        print(f"Result: {resp.message}")

if __name__ == "__main__":
    run()
