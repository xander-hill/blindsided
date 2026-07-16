import React, { useState } from 'react';
import { auctionClient } from '../services/AuctionClient';

const createRequestId = () =>
  globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random()}`;

export const BidForm = ({ auctionId }: { auctionId: string }) => {
  const [amount, setAmount] = useState(0);
  const [status, setStatus] = useState('');

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setStatus('Transmitting...');

    try {
      const { response } = await auctionClient.placeBid({
        auctionId,
        bidderId: "bidder_01",
        amount,
        expectedVersion: 0,
        requestId: createRequestId()
      });

      setStatus(response.success ? "✅ Bid Vaulted" : `⚠️ ${response.message}`);
    } catch (err: any) {
      setStatus(`❌ Error: ${err.message}`);
    }
  };

  return (
    <form onSubmit={handleSubmit} style={{ display: 'flex', flexDirection: 'column', gap: '10px' }}>
      <input 
        type="number" 
        placeholder="Bid Amount" 
        onChange={e => setAmount(Number(e.target.value))} 
        style={{ padding: '8px', background: '#0d1117', color: 'white', border: '1px solid #444' }}
      />
      <button type="submit" style={{ padding: '8px', cursor: 'pointer' }}>Vault Bid</button>
      {status && <p style={{ fontSize: '0.8rem' }}>{status}</p>}
    </form>
  );
};
