import { AuctionOutcome, AuctionState } from '../../proto/blindsided'
import type { PublicAuction, AuctionResult } from '../../proto/blindsided'
import type { WatchState } from '../../types/demo'

interface Props {
  auction: PublicAuction | null
  version: number
  bidderCount: number
  ownBid?: number
  result?: AuctionResult
  watchState: WatchState
  busy: string
  message: string
  simulationRunning: boolean
  writesAvailable: boolean
  onCreate: () => void
  onBid: (amount: number) => void
  onWithdraw: () => void
  onReveal: () => void
  onStartSimulation: () => void
  onStopSimulation: () => void
}

const money = (value: number) => new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD',
}).format(value)

function Outcome({ result, bidderCount }: { result?: AuctionResult; bidderCount: number }) {
  if (!result) return <p className="muted">Final outcome is not available.</p>
  const label = result.outcome === AuctionOutcome.NO_BIDS
    ? 'No bids received'
    : result.outcome === AuctionOutcome.RESERVE_NOT_MET
      ? 'Reserve not met'
      : 'Successful sale'
  return (
    <div className="outcome" data-testid="final-outcome">
      <span className="eyebrow">Final outcome</span>
      <h3>{label}</h3>
      <p>{result.reserveMet ? 'Reserve met' : 'Reserve not met'} · {bidderCount} final bidders</p>
      {result.hasWinner && result.winningBidderId && result.winningAmount !== undefined && (
        <p className="winner">Winner <strong>{result.winningBidderId}</strong> at <strong>{money(result.winningAmount)}</strong></p>
      )}
    </div>
  )
}

export function AuctionPanel(props: Props) {
  const open = props.auction?.state === AuctionState.OPEN
  const disabled = Boolean(props.busy) || !props.writesAvailable || props.version < 1
  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    const form = new FormData(event.currentTarget)
    const amount = Number(form.get('amount'))
    if (Number.isFinite(amount) && amount > 0) props.onBid(amount)
  }

  return (
    <section className="panel auction-panel">
      <div className="section-heading">
        <div><span className="eyebrow">Auction simulation</span><h2>The sealed room</h2></div>
        {props.auction && <span className={`status ${open ? 'ready' : 'revealed'}`} data-testid="auction-state">{open ? 'OPEN' : 'REVEALED'}</span>}
      </div>
      {!props.auction ? (
        <div className="empty-state">
          <div className="gavel-mark">B</div>
          <h3>One auction. Real failure modes.</h3>
          <p>Create the opinionated demo, generate blind bid traffic, then pull replicas out from underneath it.</p>
          <button className="primary" onClick={props.onCreate} disabled={Boolean(props.busy)}>
            {props.busy === 'create' ? 'Creating…' : 'Create demo auction'}
          </button>
        </div>
      ) : (
        <>
          <div className="auction-meta">
            <div><span className="eyebrow">{props.auction.category}</span><h3>{props.auction.title}</h3><p>{props.auction.description}</p></div>
            <dl className="facts">
              <div><dt>Version</dt><dd data-testid="auction-version">{props.version || '—'}</dd></div>
              <div><dt>Active bidders</dt><dd data-testid="bidder-count">{props.bidderCount}</dd></div>
              <div><dt>Your sealed bid</dt><dd>{props.ownBid === undefined ? 'None' : money(props.ownBid)}</dd></div>
            </dl>
          </div>
          <div className="watch-line">
            <span className={`signal ${props.watchState}`} />
            Watch stream <strong data-testid="watch-status">{props.watchState}</strong>
            <span className="privacy-note">Amounts remain sealed until reveal</span>
          </div>
          {open ? (
            <>
              <form className="bid-form" onSubmit={submit}>
                <label htmlFor="amount">{props.ownBid === undefined ? 'Your bid' : 'Replace your bid'}</label>
                <div className="input-row"><span>$</span><input id="amount" name="amount" type="number" min="1" step=".01" defaultValue={props.ownBid ? props.ownBid + 50 : 900} /><button className="primary" disabled={disabled}>{props.busy === 'bid' ? 'Committing…' : props.ownBid === undefined ? 'Place sealed bid' : 'Replace sealed bid'}</button></div>
              </form>
              <div className="action-row">
                <button onClick={props.onWithdraw} disabled={disabled || props.ownBid === undefined}>Withdraw bid</button>
                <button onClick={props.simulationRunning ? props.onStopSimulation : props.onStartSimulation} disabled={!props.writesAvailable}>
                  {props.simulationRunning ? 'Stop simulated bidders' : 'Start simulated bidders'}
                </button>
                <button className="danger-text" onClick={props.onReveal} disabled={disabled}>{props.busy === 'reveal' ? 'Revealing…' : 'Reveal auction'}</button>
              </div>
            </>
          ) : <Outcome result={props.result ?? props.auction.result} bidderCount={props.bidderCount} />}
        </>
      )}
      {props.message && <div className="notice" role="status">{props.message}</div>}
    </section>
  )
}
