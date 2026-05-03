// frontend/src/hooks/useLiveAuction.ts
import { useState, useEffect } from 'react';
import { auctionClient } from '../services/AuctionClient';

export function useLiveAuction(auctionId: string) {
  const [data, setData] = useState<any>(null);

  useEffect(() => {
    if (!auctionId) return;

    const abortController = new AbortController();
    let isMounted = true; // Track if the component is still alive

    async function runStream() {
        console.log("📡 Attempting to open gRPC Stream...");
        try {
        const stream = auctionClient.joinLiveAuction({
            auctionId,
            userId: "user_123"
        }, { abort: abortController.signal });

        for await (const response of stream.responses) {
            if (!isMounted) break; 
            console.log("✅ Update received:", response);
            setData(response);
        }
        } catch (err: any) {
        // Only log the error if we didn't intentionally abort it
        if (err.name !== 'AbortError' && isMounted) {
            console.error("❌ Stream crashed:", err);
        }
        }
    }

    runStream();

    return () => {
        isMounted = false;
        abortController.abort();
    };
    }, [auctionId]);

  return { data };
}