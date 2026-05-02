import grpc
import time
from proto.src import marketplace_pb2 as pb2
from proto.src import marketplace_pb2_grpc as pb2_grpc

# Assuming Service Node is exposed on localhost:50051 for local testing
# In K8s, this would be your Service LoadBalancer IP/DNS
SERVICE_ADDRESS = "localhost:50051"

def run_test():
    with grpc.insecure_channel(SERVICE_ADDRESS) as channel:
        stub = pb2_grpc.MarketplaceStub(channel)
        item_id = f"item_{int(time.time())}"

        print("--- STEP 1: Creating a Blind Auction ---")
        create_res = stub.CreateItem(pb2.CreateItemRequest(
            item=pb2.Item(
                item_id=item_id,
                seller_id="seller_1",
                title="Antique Relic",
                description="A mysterious artifact.",
                starting_price=100.0,
                highest_bid=100.0, # Initialize with starting price
                is_closed=False
            )
        ))
        print(f"Create Result: {create_res.message}\n")

        print("--- STEP 2: Placing a Secret Bid ($150) ---")
        bid_res = stub.PlaceBid(pb2.BidRequest(
            item_id=item_id,
            buyer_id="buyer_A",
            amount=150.0,
            expected_version=1
        ))
        print(f"Bid Result: {bid_res.message} (Success: {bid_res.success})")
        # In a blind auction, current_price should be masked or starting_price
        print(f"Masked Price returned: {bid_res.current_price}\n")

        print("--- STEP 3: Verifying the 'Fog of War' (Query) ---")
        query_res = stub.QueryItems(pb2.QueryRequest(filter=item_id))
        if query_res.items:
            item = query_res.items[0]
            print(f"Item Status: {'CLOSED' if item.is_closed else 'OPEN'}")
            print(f"Visible Bid: {item.highest_bid} (Should be 0.0 or hidden)")
            print(f"Visible Bidder: {item.highest_bidder_id} (Should be 'HIDDEN')\n")

        print("--- STEP 4: Attempting a 'Low' Bid ($120) ---")
        # We expect this to fail because the secret high bid is $150
        low_bid = stub.PlaceBid(pb2.BidRequest(
            item_id=item_id,
            buyer_id="buyer_B",
            amount=120.0,
            expected_version=2
        ))
        print(f"Low Bid Result: {low_bid.message} (Success: {low_bid.success})\n")

        print("--- STEP 5: Dropping the Gavel (The Reveal) ---")
        # Repurposing UpdateItem to flip the status to CLOSED
        close_res = stub.UpdateItem(pb2.UpdateItemRequest(
            item_id=item_id,
            seller_id="seller_1",
            status="CLOSED",
            expected_version=2 
        ))
        print(f"Close Result: {close_res.message}\n")

        print("--- STEP 6: Final Verification (The Reveal) ---")
        final_query = stub.QueryItems(pb2.QueryRequest(filter=item_id))
        if final_query.items:
            item = final_query.items[0]
            print(f"Winning Bid: {item.highest_bid} (Should be 150.0)")
            print(f"Winner ID: {item.highest_bidder_id} (Should be 'buyer_A')")

if __name__ == "__main__":
    run_test()