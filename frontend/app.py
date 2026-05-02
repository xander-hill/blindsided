import streamlit as st
import grpc
import logging
import sys
from proto.src import blindsided_pb2 as pb2
from proto.src import blindsided_pb2_grpc as pb2_grpc

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("BlindSidedUI")

SERVICE_ADDR = 'localhost:50051'

st.set_page_config(page_title="BlindSided Vault", page_icon="🌫️", layout="wide")

def get_stub():
    channel = grpc.insecure_channel(SERVICE_ADDR)
    return pb2_grpc.BlindSidedStub(channel)

# --- APP UI ---
st.title("🌫️ BlindSided: The Live Vault")

with st.sidebar:
    st.header("🏢 Auction Control")
    auc_id = st.text_input("Auction ID", value="demo_vault_1")
    
    if st.button("🌟 Initialize Vault"):
        logger.info(f"Attempting to initialize auction: {auc_id}")
        try:
            stub = get_stub()
            res = stub.OpenAuction(pb2.OpenRequest(auction=pb2.Auction(
                auction_id=auc_id, title="Ancient GPU Cluster", reserve_price=500.0
            )))
            if res.ok:
                logger.info(f"Successfully opened auction {auc_id}")
                st.success(f"Auction {auc_id} is LIVE.")
            else:
                logger.warning(f"Vault rejected OpenAuction: {res.message}")
                st.error(res.message)
        except Exception as e:
            logger.error(f"Error opening vault: {e}")
            st.error(str(e))

    if st.button("🔨 DROP THE GAVEL"):
        logger.info(f"Gavel command issued for {auc_id}")
        try:
            get_stub().DropTheGavel(pb2.GavelRequest(auction_id=auc_id, expected_version=-1))
        except Exception as e:
            logger.error(f"Gavel error: {e}")

# --- MAIN: Bidding ---
left_col, right_col = st.columns([1, 1])

with left_col:
    st.subheader("💰 Place a Secret Bid")
    bidder_name = st.text_input("Your Name", value="Xander")
    bid_amt = st.number_input("Bid Amount ($)", min_value=0.0, step=10.0)
    
    if st.button("Submit Bid into the Fog"):
        logger.info(f"User {bidder_name} attempting bid of ${bid_amt} on {auc_id}")
        try:
            stub = get_stub()
            # FETCH VERSION BEFORE BIDDING (Prevents Stale Version error)
            status = stub.GetStatus(pb2.StatusRequest(auction_id=auc_id))
            current_v = status.auction.version if status.ok else 0
            logger.info(f"Current version detected in Vault: v{current_v}")

            res = stub.PlaceSecretBid(pb2.BidRequest(
                auction_id=auc_id, 
                buyer_id=bidder_name, 
                amount=bid_amt, 
                expected_version=current_v
            ))
            
            if res.success:
                logger.info(f"Bid accepted. Vault should increment to v{current_v + 1}")
                st.balloons()
            else:
                logger.warning(f"Bid rejected by Judge: {res.message}")
                st.error(res.message)
        except Exception as e:
            logger.error(f"RPC Failure during bid: {e}")

with right_col:
    st.subheader("🔍 Live Vault Watcher")
    status_box = st.empty()
    
    if st.button("📡 Connect to Stream"):
        logger.info(f"Opening live stream for {auc_id}...")
        try:
            stub = get_stub()
            for update in stub.JoinLiveAuction(pb2.AuctionRequest(auction_id=auc_id)):
                logger.info(f"[STREAM] Update received: revealed={update.is_revealed}, msg='{update.message}'")
                
                status_box.markdown(f"""
                **Status:** {update.message}  
                **Revealed:** `{update.is_revealed}`  
                **Current Price:** `${update.revealed_price}`  
                """)
                
                if update.is_revealed:
                    logger.info("Gavel detected in stream. Closing connection.")
                    st.snow()
                    break
        except Exception as e:
            logger.error(f"Stream interrupted: {e}")