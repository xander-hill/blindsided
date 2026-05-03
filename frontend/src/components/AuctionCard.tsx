import React from 'react';
import { useLiveAuction } from '../hooks/useLiveAuction';

export const AuctionCard = ({ id }: { id: string }) => {
  const { data, loading } = useLiveAuction(id);

  if (loading) return <div>Synchronizing with Vault...</div>;
  if (!data) return <div>No data available for this Auction.</div>;

  return (
    <div style={{ border: '1px solid #444', padding: '20px', borderRadius: '8px' }}>
      <h3>Auction ID: {id}</h3>
      
      {data.isRevealed ? (
        <div style={{ color: '#00ff00' }}>
          <h4>🔨 GAVEL FELL</h4>
          <p>Winner: {data.winnerId}</p>
          <p>Price: ${data.finalPrice}</p>
        </div>
      ) : (
        <div>
          <p>Bidders: {data.bidderCount}</p>
          <div style={{ background: '#222', height: '10px', width: '100%' }}>
             {/* Simple visualization of the opaque range */}
             <div style={{ 
                marginLeft: `${(data.lowRange / 1000) * 100}%`, 
                width: `${((data.highRange - data.lowRange) / 1000) * 100}%`,
                background: 'orange', height: '100%' 
             }}></div>
          </div>
          <p>Opaque Range: ${data.lowRange} - ${data.highRange}</p>
          {data.reserveStatus && <span style={{color: 'gold'}}>Reserve Met</span>}
        </div>
      )}
    </div>
  );
};