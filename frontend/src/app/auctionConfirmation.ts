import { AuctionState } from '../proto/blindsided'
import type { AuctionUpdate, PublicAuction } from '../proto/blindsided'

export type PendingConfirmation =
  | { kind: 'bid'; success: string; minimumVersion: number; amount: number }
  | { kind: 'withdraw'; success: string; minimumVersion: number }
  | { kind: 'reveal'; success: string; minimumVersion: number }

interface ObservedAuction {
  update: AuctionUpdate | null
  auction: PublicAuction | null
  ownBid?: number
  authoritativeReadCompleted: boolean
}

const sameAmount = (left: number, right: number) =>
  Math.abs(left - right) < 0.005

export function confirmationObserved(
  pending: PendingConfirmation,
  observed: ObservedAuction,
): boolean {
  if (pending.kind === 'reveal') {
    const watchConfirmed = Boolean(
      observed.update
      && observed.update.version >= pending.minimumVersion
      && observed.update.state === AuctionState.REVEALED,
    )
    const readConfirmed = observed.authoritativeReadCompleted
      && observed.auction?.state === AuctionState.REVEALED
    return watchConfirmed || readConfirmed
  }

  // WatchAuction deliberately keeps bid amounts sealed, so bid/withdrawal
  // confirmation must come from the caller-scoped authoritative read.
  if (!observed.authoritativeReadCompleted) return false
  return pending.kind === 'bid'
    ? observed.ownBid !== undefined && sameAmount(observed.ownBid, pending.amount)
    : observed.ownBid === undefined
}
