import { useEffect, useRef, useState } from 'react'
import { AuctionState } from '../proto/blindsided'
import type { AuctionUpdate } from '../proto/blindsided'
import { watchAuction } from '../services/auctionClient'
import type { WatchState } from '../types/demo'

export function useAuctionWatch(auctionId: string | null) {
  const [update, setUpdate] = useState<AuctionUpdate | null>(null)
  const [state, setState] = useState<WatchState>('disconnected')
  const reconnects = useRef(0)

  useEffect(() => {
    setUpdate(null)
    if (!auctionId) {
      setState('disconnected')
      return
    }
    const controller = new AbortController()
    let active = true
    let timer: number | undefined
    let latestState: AuctionState | undefined

    const connect = async () => {
      setState(reconnects.current ? 'reconnecting' : 'disconnected')
      try {
        for await (const next of watchAuction(auctionId, controller.signal)) {
          if (!active) return
          reconnects.current = 0
          setState('connected')
          latestState = next.state
          setUpdate(next)
        }
        if (!active) return
        if (latestState === AuctionState.REVEALED) {
          setState('complete')
          return
        }
        throw new Error('Watch ended before the auction was revealed')
      } catch (error) {
        if (!active || controller.signal.aborted) return
        reconnects.current += 1
        setState(reconnects.current > 4 ? 'error' : 'reconnecting')
        timer = window.setTimeout(connect, Math.min(1000 * 2 ** reconnects.current, 8000))
        console.warn('Auction watch reconnecting', error)
      }
    }
    void connect()
    return () => {
      active = false
      controller.abort()
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [auctionId])

  return { update, state }
}
