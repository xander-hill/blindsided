// frontend/src/hooks/useLiveAuction.ts
import { useState, useEffect } from 'react';
import { auctionClient } from '../services/AuctionClient';

export function useLiveAuction(auctionId: string) {
  const [data, setData] = useState<any>(null);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    if (!auctionId) {
      setLoading(false);
      return;
    }

    const abortController = new AbortController();
    let isMounted = true; // Track if the component is still alive
    setLoading(true);

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
            setLoading(false);
        }
        } catch (err: any) {
        // Only log the error if we didn't intentionally abort it
        if (err.name !== 'AbortError' && isMounted) {
            console.error("❌ Stream crashed:", err);
        }
        if (isMounted) {
            setLoading(false);
        }
        }
    }

    runStream();

    return () => {
        isMounted = false;
        abortController.abort();
    };
    }, [auctionId]);

  return { data, loading };
}
