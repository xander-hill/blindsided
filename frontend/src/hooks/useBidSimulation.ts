import { useEffect, useRef, useState } from 'react'
import { getAuction, placeBid, withdrawBid } from '../services/auctionClient'
import { AuctionState } from '../proto/blindsided'

const identities = Array.from({ length: 8 }, (_, index) => `sim-bidder-${index + 1}`)

export function useBidSimulation(
  auctionId: string | null,
  version: number,
  enabled: boolean,
  writesAvailable: boolean,
) {
  const [running, setRunning] = useState(false)
  const advisory = useRef(new Map<string, number>())

  useEffect(() => {
    if (!running || !auctionId || !enabled) return
    let active = true
    let timer: number
    const tick = async () => {
      if (!active) return
      if (!writesAvailable) {
        timer = window.setTimeout(tick, 2200)
        return
      }
      try {
        const bidder = identities[Math.floor(Math.random() * identities.length)]
        const fetched = await getAuction(auctionId, bidder)
        if (fetched.auction?.state !== AuctionState.OPEN) { setRunning(false); return }
        const own = fetched.ownActiveBidAmount
        const roll = Math.random()
        if (roll > .9 && own !== undefined) {
          await withdrawBid(auctionId, bidder, version)
          advisory.current.delete(bidder)
        } else {
          const current = own ?? advisory.current.get(bidder) ?? 500
          const amount = Math.round((current + 20 + Math.random() * 180) * 100) / 100
          await placeBid(auctionId, bidder, amount, version)
          advisory.current.set(bidder, amount)
        }
      } catch {
        // The server is authoritative; conflicts and transient outages are skipped.
      }
      if (active) timer = window.setTimeout(tick, 600 + Math.random() * 900)
    }
    void tick()
    return () => { active = false; window.clearTimeout(timer) }
  }, [auctionId, enabled, running, version, writesAvailable])

  return { running: running && enabled, start: () => setRunning(true), stop: () => setRunning(false) }
}
