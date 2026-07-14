# Save as seed_auction.py
import grpc
from google.protobuf import timestamp_pb2
from backend.blindsided.generated import blindsided_pb2 as pb2
from backend.blindsided.generated import blindsided_pb2_grpc as pb2_grpc


def future_timestamp():
    return timestamp_pb2.Timestamp(seconds=4102444800)


def run():
    # We hit Envoy, which we know works based on your previous logs
    with grpc.insecure_channel('localhost:8080') as channel:
        stub = pb2_grpc.AuctionServiceStub(channel)
        
        request = pb2.CreateAuctionRequest(
            seller_id="seller-rolex",
            title="Vintage Rolex Submariner",
            description="Reference 5513, ghost bezel, original tritium dial.",
            reserve_price=12000.0,
            ends_at=future_timestamp(),
        )
        
        print("🌱 Seeding auction 'vintage-rolex'...")
        resp = stub.CreateAuction(request)
        print(f"Result: {resp.message} ({resp.auction_id})")
        bid = stub.PlaceBid(pb2.BidRequest(
            auction_id=resp.auction_id,
            bidder_id="house_bidder",
            amount=8500.0,
            expected_version=1,
        ))
        print(f"Seed bid: {bid.message}")

if __name__ == "__main__":
    run()
