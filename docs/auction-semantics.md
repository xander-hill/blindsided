# Auction Semantics

## 1. Purpose

This document defines the behavioral contract for the Blindsided auction
system. It specifies the domain rules and distributed-system guarantees
that all implementations must satisfy. Where implementation and contract
differ, the contract is authoritative.

---

## 2. Auction Model

### 2.1 Lifecycle

An auction has exactly two lifecycle states:

- `OPEN`
- `REVEALED`

Rules:

- An auction begins in the `OPEN` state.
- An auction transitions from `OPEN` to `REVEALED` at most once.
- `REVEALED` is a terminal state.
- A revealed auction rejects all further mutations.
- The system performs the reveal transition. It may be initiated by an
  authorized request or automatically when the auction deadline is
  reached.

### 2.2 Auction Creation

A successful creation establishes:

- a unique auction identifier
- seller identity
- immutable closing timestamp (`ends_at`)
- reserve price
- an empty active bid collection
- lifecycle state `OPEN`
- initial auction version

### 2.3 Reserve Price

The reserve price is an auction property, not a bid.

Rules:

- It is configured by the seller during auction creation.
- It is not associated with a bidder.
- It does not count toward bidder count.
- It cannot become the winning bid.
- It is never exposed to external bidders before reveal.

---

## 3. Bid Semantics

### 3.1 Active Bid

Each bidder may have at most one active bid per auction.

### 3.2 Bid Submission

- A bidder may submit one active bid.
- A bidder may not lower an active bid directly.
- Successful bid mutations increment the auction version exactly once.
- Rejected bids do not modify auction state or version.

### 3.3 Bid Withdrawal

While the auction is open and before `ends_at`, a bidder may withdraw
their own active bid.

A successful withdrawal:

- removes the bidder's active bid
- decreases the distinct bidder count
- recalculates internal auction state
- increments the auction version exactly once

After withdrawal, the bidder may submit a new bid at any valid amount
while the auction remains open.

### 3.4 Tie Breaking

If multiple active bidders share the highest bid amount, the earliest
accepted active bid wins.

Acceptance order is assigned by the system and must remain deterministic
across replication, restart, and failover.

---

## 4. Visibility

### 4.1 Before Reveal

External users may see:

- auction metadata
- auction state
- distinct active bidder count

External users must not see:

- bid amounts
- bidder identities
- reserve price
- reserve status
- leading bid
- winning bidder
- winning amount

### 4.2 After Reveal

External users may see:

- whether the reserve was met
- whether the auction produced a winner
- winning bidder
- winning amount
- final bidder count

External users must not see:

- losing bidder identities
- losing bid amounts
- complete bid history

---

## 5. Auction Outcome

An auction has a winner if and only if:

- at least one valid bid was accepted
- the highest accepted bid meets or exceeds the reserve price

Possible outcomes:

- No bids received
- Reserve not met
- Successful sale

If the reserve is not met, no winner is published.

---

## 6. Optimistic Concurrency

Mutations use optimistic concurrency internally.

If a mutation encounters a stale version, the service may retry the
logical request against the latest authoritative state.

Each retry must revalidate all domain rules.

Retries are bounded.

If retries are exhausted, the system returns a specific concurrency
conflict response.

---

## 7. Idempotency

Every mutation request carries a unique request identifier.

The system guarantees at-most-once application for a given request
identifier.

Repeated requests with the same identifier return the original result
without applying the mutation again.

Reusing a request identifier with different request contents is
rejected.

---

## 8. Read Consistency

Authoritative reads use the current primary.

These include:

- single-auction status
- bidder count
- bidder's own active bid
- reveal status
- auction outcome
- reads used by mutations
- live auction updates

Replica reads are permitted only for stale-tolerant discovery operations
such as search and listings.

---

## 9. Write Acknowledgement

A mutation is acknowledged only after it has been committed to:

- the primary
- the designated synchronous backup

If this acknowledgement requirement cannot be satisfied:

- the mutation is not committed
- the auction version does not advance
- the client receives a failure response

---

## 10. Failover

### Promotion Eligibility

Only a backup known to contain the complete committed state may become
primary.

### Promotion Barrier

Before accepting writes, a promoted primary must:

- receive the current epoch
- confirm complete committed state
- establish a synchronized backup
- complete promotion readiness

### Stale Primary Protection

Every primary assignment is associated with a monotonically increasing
epoch.

Mutations and replication requests from older epochs are rejected.

A former primary must synchronize before becoming eligible for promotion
again.

### In-Flight Requests

Interrupted mutations may be retried only after the new primary is fully
ready.

Retries must preserve the original request identifier.

### Overdue Auctions

Deadlines remain authoritative during failover.

After promotion, the new primary finalizes overdue auctions only after
validating:

- current epoch
- complete committed state
- synchronized backup
- overdue status
- idempotency

---

## 11. Authoritative Time

All deadline comparisons use the authoritative server time of the
current primary.

Client clocks are never authoritative.

---

## 12. Invariants

The following statements must always hold:

- An auction has exactly one lifecycle state.
- An auction transitions from `OPEN` to `REVEALED` at most once.
- A revealed auction never accepts another mutation.
- Each bidder has at most one active bid.
- Every successful mutation increments the auction version exactly
  once.
- Every committed auction has exactly one authoritative history.
- A committed mutation is never lost during supported failover.
- A stale primary never accepts authoritative writes.
- A winner exists if and only if the reserve is met.
- Equal winning bids are resolved deterministically.
- Every mutation is applied at most once for a given request
  identifier.
