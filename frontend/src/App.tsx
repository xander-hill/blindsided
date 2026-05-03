import React, { useState } from 'react';
import { AuctionCard } from './components/AuctionCard';
import { BidForm } from './components/BidForm';
import './App.css';

function App() {
  const [activeAuctionId, setActiveAuctionId] = useState<string | null>(null);
  const [searchInput, setSearchInput] = useState("");

  const handleJoin = (e: React.FormEvent) => {
    e.preventDefault();
    if (searchInput.trim()) {
      setActiveAuctionId(searchInput.trim());
    }
  };

  return (
    <div className="app-container">
      <header>
        <h1>🌑 BlindSided</h1>
        <p>Bid in the Dark. Win in the Light.</p>
      </header>

      <main>
        {!activeAuctionId ? (
          /* LOBBY VIEW */
          <div className="lobby">
            <form onSubmit={handleJoin}>
              <input 
                type="text" 
                placeholder="Enter Auction ID (e.g., 'vintage-rolex')" 
                value={searchInput}
                onChange={(e) => setSearchInput(e.target.value)}
              />
              <button type="submit">Enter the Vault</button>
            </form>
          </div>
        ) : (
          /* LIVE VAULT VIEW */
          <div className="vault-view">
            <button className="back-btn" onClick={() => setActiveAuctionId(null)}>
              ← Back to Lobby
            </button>
            
            <div className="grid">
              {/* THE LIVE STREAM COMPONENT */}
              <AuctionCard id={activeAuctionId} />
              
              {/* THE INTERACTION COMPONENT */}
              <BidForm auctionId={activeAuctionId} />
            </div>
          </div>
        )}
      </main>
    </div>
  );
}

export default App;
