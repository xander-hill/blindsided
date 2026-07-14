import grpc
from backend.blindsided.generated import blindsided_pb2 as pb2
from backend.blindsided.generated import blindsided_pb2_grpc as pb2_grpc

def place_bid(amount, bidder_id):
    # Connect through Envoy port-forward
    with grpc.insecure_channel('localhost:8080') as channel:
        stub = pb2_grpc.AuctionServiceStub(channel)
        
        print(f"🚀 Sending bid for ${amount} from {bidder_id}...")
        
        # PlaceBid expects auction_id, bidder_id, amount, and expected_version
        # Note: Set expected_version to 0 to bypass strict OCC check for this test
        request = pb2.BidRequest(
            auction_id="vintage-rolex",
            bidder_id=bidder_id,
            amount=amount,
            expected_version=0 
        )
        
        resp = stub.PlaceBid(request)
        print(f"✅ Result: {resp.message}")

if __name__ == "__main__":
    # Change these values to "up" the bid!
    place_bid(amount=11500.0, bidder_id="speedmaster_fan_99")