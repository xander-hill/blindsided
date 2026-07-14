import grpc
import threading
import random
import time
from google.protobuf import timestamp_pb2
from backend.blindsided.generated import blindsided_pb2 as pb2
from backend.blindsided.generated import blindsided_pb2_grpc as pb2_grpc

# CONFIG
AUCTION_ID = f"chaos_test_{int(time.time())}"
NUM_BIDDERS = 50
BIDS_PER_BIDDER = 10
SERVICE_ADDR = 'localhost:50051'


def future_timestamp():
    return timestamp_pb2.Timestamp(seconds=4102444800)


def hammer_the_vault(bidder_id):
    channel = grpc.insecure_channel(SERVICE_ADDR)
    stub = pb2_grpc.AuctionServiceStub(channel)
    
    for i in range(BIDS_PER_BIDDER):
        success = False
        retries = 0
        while not success and retries < 5:
            try:
                # Add a short timeout so a single call can't hang the thread forever
                status = stub.GetAuction(pb2.GetAuctionRequest(auction_id=AUCTION_ID), timeout=2.0)
                v = status.auction.version
                
                resp = stub.PlaceBid(pb2.BidRequest(
                    auction_id=AUCTION_ID,
                    bidder_id=bidder_id,
                    amount=random.uniform(100, 1000),
                    expected_version=v
                ), timeout=2.0)
                
                if resp.success:
                    success = True
                    # PULSE: Print every few successful bids so you know it's alive
                    if i % 5 == 0:
                        print(f"  [Progress] {bidder_id} -> Bid {i} landed.")
                else:
                    retries += 1
                    time.sleep(random.uniform(0.05, 0.1)) # Slightly longer backoff
            except Exception as e:
                print(f"  [!] {bidder_id} error: {e}")
                break

def run_chaos_test():
    global AUCTION_ID

    channel = grpc.insecure_channel(SERVICE_ADDR)
    stub = pb2_grpc.AuctionServiceStub(channel)

    print(f"🔥 Starting Chaos Test: {NUM_BIDDERS} bidders, {BIDS_PER_BIDDER} bids each.")
    
    # 1. Open the Auction
    opened = stub.CreateAuction(pb2.CreateAuctionRequest(
        seller_id="seller-chaos",
        title="Chaos Item",
        reserve_price=700.0,
        ends_at=future_timestamp(),
    ))
    AUCTION_ID = opened.auction_id
    print(f"Generated Auction ID: {AUCTION_ID}")

    threads = []
    start_time = time.time()

    # 2. Launch the Swarm
    for i in range(NUM_BIDDERS):
        t = threading.Thread(target=hammer_the_vault, args=(f"User_{i}",))
        threads.append(t)
        t.start()

    for t in threads:
        t.join()

    end_time = time.time()
    
    # 3. Final Reconciliation
    print("\n--- 🏁 Chaos Test Complete ---")
    print(f"Total Time: {end_time - start_time:.2f} seconds")
    
    # Check the state
    final_status = stub.GetAuction(pb2.GetAuctionRequest(auction_id=AUCTION_ID))
    
    # We join the stream once to get the opaque stats
    stream = stub.WatchAuction(pb2.AuctionRequest(auction_id=AUCTION_ID))
    opaque_data = next(stream)

    print(f"Final Version in Vault: {final_status.auction.version}")
    print(f"Unique Bidders Found: {opaque_data.bidder_count}")
    print(f"Final Opaque Range: ${opaque_data.low_range} - ${opaque_data.high_range}")
    
    if opaque_data.bidder_count == NUM_BIDDERS:
        print("✅ SUCCESS: Map-merge handled concurrency. No data loss.")
    else:
        print(f"❌ FAILURE: Expected {NUM_BIDDERS} unique bidders, but found {opaque_data.bidder_count}.")

if __name__ == "__main__":
    run_chaos_test()
