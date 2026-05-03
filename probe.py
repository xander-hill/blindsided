import grpc
from proto import blindsided_pb2 as pb2
from proto import blindsided_pb2_grpc as pb2_grpc

def check_vault():
    try:
        # We hit your Envoy port-forward
        with grpc.insecure_channel('localhost:8080') as channel:
            stub = pb2_grpc.BlindSidedStub(channel)
            resp = stub.SearchAuctions(pb2.SearchRequest(query="vintage-rolex"))
            if resp.ok:
                print(f"✅ Found {len(resp.auctions)} auctions.")
                for a in resp.auctions:
                    print(f"   - Auction: {a.auction_id}, Title: {a.title}")
            else:
                print(f"❌ Search failed: {resp.message}")
    except Exception as e:
        print(f"💥 Connection Error: {e}")

if __name__ == "__main__":
    check_vault()