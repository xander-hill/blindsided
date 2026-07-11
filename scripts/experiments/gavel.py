import grpc
from backend.blindsided.generated import blindsided_pb2 as pb2
from backend.blindsided.generated import blindsided_pb2_grpc as pb2_grpc

def drop_the_gavel():
    auction_id = "vintage-rolex"
    
    with grpc.insecure_channel('localhost:8080') as channel:
        stub = pb2_grpc.BlindSidedStub(channel)
        
        print(f"🔨 Dropping the Gavel for {auction_id}...")
        
        # 1. Trigger the reveal event
        gavel_req = pb2.GavelRequest(auction_id=auction_id)
        gavel_resp = stub.DropTheGavel(gavel_req)
        
        if gavel_resp.ok:
            print("✅ The Vault has been opened! Truth revealed.")
            
            # 2. Fetch the FINAL status to see the winner details
            # In the 'Revealed' state, the Service Node stops masking the data
            status_resp = stub.GetStatus(pb2.StatusRequest(auction_id=auction_id))
            
            if status_resp.ok:
                auction = status_resp.auction
                # Find the winner from the bids map
                if auction.bids:
                    winner_id = max(auction.bids, key=auction.bids.get)
                    winning_price = auction.bids[winner_id]
                    
                    print("\n--- 📜 FINAL AUCTION LOG ---")
                    print(f"Auction ID:    {auction.auction_id}")
                    print(f"Status:        REVEALED")
                    print(f"Winner:        {winner_id}")
                    print(f"Final Price:   ${winning_price:,.2f}")
                    print(f"Total Bidders: {len(auction.bids)}")
                    print("----------------------------\n")
        else:
            print(f"❌ Failed to drop gavel: {gavel_resp.message}")

if __name__ == "__main__":
    drop_the_gavel()