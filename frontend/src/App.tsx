import { useCallback, useEffect, useMemo, useState } from 'react'
import { config } from './app/config'
import { AuctionPanel } from './components/auction/AuctionPanel'
import { ControlRoom } from './components/cluster/ControlRoom'
import { useAuctionWatch } from './hooks/useAuctionWatch'
import { useBidSimulation } from './hooks/useBidSimulation'
import { useClusterStatus } from './hooks/useClusterStatus'
import { AuctionState } from './proto/blindsided'
import type { PublicAuction } from './proto/blindsided'
import { createDemoAuction, getAuction, placeBid, revealAuction, withdrawBid } from './services/auctionClient'
import { getSystemEvents, runDemoAction } from './services/demoControlClient'
import type { DemoAction, DemoEvent } from './types/demo'
import './index.css'

const event = (title: string, category: DemoEvent['category'], severity: DemoEvent['severity'] = 'info', detail?: string): DemoEvent => ({
  id: crypto.randomUUID(), timestamp: new Date().toISOString(), category, title, severity, detail,
})

export default function App() {
  const [auctionId, setAuctionId] = useState<string | null>(null)
  const [auction, setAuction] = useState<PublicAuction | null>(null)
  const [ownBid, setOwnBid] = useState<number>()
  const [busy, setBusy] = useState('')
  const [message, setMessage] = useState('')
  const [localEvents, setLocalEvents] = useState<DemoEvent[]>([])
  const [systemEvents, setSystemEvents] = useState<DemoEvent[]>([])
  const [pendingAction, setPendingAction] = useState<DemoAction | ''>('')
  const { status, error: clusterError } = useClusterStatus()
  const watch = useAuctionWatch(auctionId)
  const version = watch.update?.version ?? 0
  const bidderCount = watch.update?.bidderCount ?? auction?.bidderCount ?? 0
  const revealed = (watch.update?.state ?? auction?.state) === AuctionState.REVEALED
  const simulator = useBidSimulation(auctionId, version, !revealed, status?.writesAvailable ?? false)

  const addEvent = useCallback((next: DemoEvent) => {
    setLocalEvents(current => [next, ...current].slice(0, 50))
  }, [])

  const refreshAuction = useCallback(async () => {
    if (!auctionId) return
    const fetched = await getAuction(auctionId)
    setAuction(fetched.auction ?? null)
    setOwnBid(fetched.ownActiveBidAmount)
  }, [auctionId])

  const visibleAuction = auction && watch.update ? {
    ...auction,
    state: watch.update.state,
    bidderCount: watch.update.bidderCount,
    result: watch.update.result,
  } : auction

  useEffect(() => {
    if (!auctionId) return
    void refreshAuction().catch(() => undefined)
  }, [auctionId, refreshAuction])

  useEffect(() => {
    let active = true
    const refresh = async () => {
      try {
        const response = await getSystemEvents()
        if (active) setSystemEvents(response.events)
      } catch { /* cluster status already communicates adapter errors */ }
    }
    void refresh()
    const timer = window.setInterval(refresh, 2500)
    return () => { active = false; window.clearInterval(timer) }
  }, [])

  const execute = async (operation: string, action: () => Promise<void>, success: string) => {
    setBusy(operation); setMessage('')
    try {
      const observedBefore = watch.receivedAt
      await action()
      await refreshAuction()
      setMessage(observedBefore === watch.receivedAt ? `${success}. Awaiting watch confirmation.` : success)
      addEvent(event(success, 'auction', 'success'))
    } catch (cause) {
      const detail = cause instanceof Error ? cause.message : 'Action failed'
      setMessage(`${detail}. State was refreshed; retry when ready.`)
      await refreshAuction().catch(() => undefined)
    } finally { setBusy('') }
  }

  const create = async () => {
    setBusy('create'); setMessage('')
    try {
      const id = await createDemoAuction()
      setAuctionId(id)
      addEvent(event('Demo auction created', 'auction', 'success', 'Blind bid visibility is active'))
    } catch (cause) {
      setMessage(cause instanceof Error ? cause.message : 'Creation failed')
    } finally { setBusy('') }
  }

  const reset = () => {
    simulator.stop()
    setAuctionId(null); setAuction(null); setOwnBid(undefined); setMessage('')
    addEvent(event('Demo view reset', 'system'))
  }

  const runAction = async (action: DemoAction) => {
    setPendingAction(action)
    try {
      const response = await runDemoAction(action)
      addEvent(event(response.message, action.includes('primary') ? 'failover' : 'replication', action.startsWith('fail') ? 'warning' : 'info'))
    } catch (cause) {
      addEvent(event(`${action.replaceAll('-', ' ')} failed`, 'system', 'critical', cause instanceof Error ? cause.message : undefined))
    } finally { setPendingAction('') }
  }

  const events = useMemo(() => [...localEvents, ...systemEvents]
    .sort((a, b) => b.timestamp.localeCompare(a.timestamp))
    .filter((item, index, all) => all.findIndex(other => other.id === item.id) === index)
    .slice(0, 50), [localEvents, systemEvents])

  return (
    <div className="app-shell">
      <header className="app-header">
        <div className="brand"><div className="brand-mark">B</div><div><h1>Blindsided</h1><p>Strongly consistent blind auctions under replica failure.</p></div></div>
        <div className="header-status">
          <span className={`status ${status?.state.toLowerCase() ?? 'unknown'}`}>{status?.state ?? 'UNKNOWN'}</span>
          <div><small>Current epoch</small><strong>{status?.epoch ?? '—'}</strong></div>
          <a className="button" href={config.grafanaUrl} target="_blank" rel="noreferrer">Open Grafana ↗</a>
          <button onClick={reset} disabled={!auctionId}>Reset demo</button>
        </div>
      </header>
      <main className="dashboard">
        <AuctionPanel
          auction={visibleAuction}
          version={version}
          bidderCount={bidderCount}
          ownBid={ownBid}
          result={watch.update?.result}
          watchState={watch.state}
          busy={busy}
          message={message}
          simulationRunning={simulator.running}
          writesAvailable={status?.writesAvailable ?? false}
          onCreate={create}
          onBid={amount => execute('bid', async () => {
            if (!auctionId) return
            await placeBid(auctionId, config.demoBidderId, amount, version)
          }, ownBid === undefined ? 'Bid committed' : 'Bid replaced')}
          onWithdraw={() => execute('withdraw', async () => {
            if (!auctionId) return
            await withdrawBid(auctionId, config.demoBidderId, version)
          }, 'Withdrawal committed')}
          onReveal={() => execute('reveal', async () => {
            if (!auctionId) return
            await revealAuction(auctionId, version)
          }, 'Auction revealed')}
          onStartSimulation={simulator.start}
          onStopSimulation={simulator.stop}
        />
        <ControlRoom status={status} error={clusterError} events={events} pendingAction={pendingAction} onAction={runAction} />
      </main>
      <footer>LOCAL DISTRIBUTED-SYSTEMS DEMONSTRATION · PUBLIC AUCTION PROJECTION ONLY</footer>
    </div>
  )
}
