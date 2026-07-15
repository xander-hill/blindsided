# Auction Specification

## 1. Purpose

This document defines the normative behavioral specification for the
Blindsided auction system. It specifies required domain behavior,
visibility constraints, concurrency semantics, and distributed-system
guarantees that conforming implementations MUST satisfy.

The key words `MUST`, `MUST NOT`, `REQUIRED`, `SHOULD`, `SHOULD NOT`,
and `MAY` are to be interpreted as normative requirement levels.

If implementation behavior, tests, ADRs, or supporting documentation
conflict with this specification, this specification is authoritative.

---

## 2. Auction Model

### 2.1 Lifecycle

An auction has exactly two lifecycle states:

- `OPEN`
- `REVEALED`

Required behavior:

- ✅ An auction MUST begin in the `OPEN` state.
- ✅ An auction MUST transition from `OPEN` to `REVEALED` no more than
  once.
- ✅ `REVEALED` MUST be a terminal state.
- ✅ A `REVEALED` auction MUST reject all subsequent state mutations.
- ✅ The system MUST perform the reveal transition.
- ✅ The reveal transition MAY be initiated by an authorized request.
- The reveal transition MAY be initiated automatically when the auction
  deadline is reached.
- ✅ The system MUST NOT transition an auction from `REVEALED` back to
  `OPEN`.

### 2.2 Auction Creation

A successful auction creation MUST establish all of the following:

- ✅ a unique auction identifier
- ✅ seller identity
- ✅ immutable closing timestamp (`ends_at`)
- ✅ reserve price
- ✅ an empty active bid collection
- ✅ lifecycle state `OPEN`
- ✅ initial auction version

Required behavior:

- ✅ The auction identifier MUST be unique across all auctions.
- ✅ The seller identity MUST be recorded as part of auction creation.
- ✅ The `ends_at` timestamp MUST NOT be mutable after creation.
- ✅ The active bid collection MUST be empty at creation.
- ✅ The initial auction version MUST be assigned by the system.
- ✅ A creation request that cannot establish every required creation
  property MUST fail without creating a partial auction.

### 2.3 Reserve Price

The reserve price is an auction property, not a bid.

Required behavior:

- ✅ The seller MUST configure the reserve price during auction creation.
- ✅ The reserve price MUST NOT be associated with any bidder.
- ✅ The reserve price MUST NOT count toward distinct active bidder count.
- ✅ The reserve price MUST NOT become the winning bid.
- ✅ The reserve price MUST NOT be exposed to external bidders before
  reveal.
- ✅ The reserve price MUST be evaluated only to determine whether the
  auction outcome is a successful sale.

---

## 3. Bid Semantics

### 3.1 Active Bid

Each bidder MAY have no more than one active bid per auction.

Required behavior:

- ✅ The system MUST enforce at most one active bid per bidder per auction.
- ✅ A new accepted bid from a bidder with an existing active bid MUST
  replace that bidder's previous active bid.
- ✅ A replaced bid MUST NOT remain eligible to win.
- ✅ A replaced bid MUST NOT count toward distinct active bidder count.

### 3.2 Bid Submission

Required behavior:

- ✅ A bidder MAY submit one active bid while the auction is `OPEN`.
- ✅ The system MUST reject bid submissions for auctions that are
  `REVEALED`.
- ✅ The system MUST reject bid submissions after `ends_at`.
- ✅ A bidder MUST NOT lower an active bid directly.
- ✅ A successful bid mutation MUST increment the auction version exactly
  once.
- ✅ A rejected bid MUST NOT modify auction state.
- ✅ A rejected bid MUST NOT modify the auction version.
- ✅ Each accepted bid MUST receive a deterministic acceptance order.

### 3.3 Bid Withdrawal

While the auction is open and before `ends_at`, a bidder MAY withdraw
their own active bid.

A successful withdrawal MUST:

- ✅ remove the bidder's active bid
- ✅ decrease the distinct active bidder count
- ✅ recalculate internal auction state
- ✅ increment the auction version exactly once

Required behavior:

- ✅ The system MUST reject withdrawal requests for auctions that are
  `REVEALED`.
- ✅ The system MUST reject withdrawal requests after `ends_at`.
- ✅ A bidder MUST NOT withdraw another bidder's active bid.
- ✅ A withdrawal request for a bidder with no active bid MUST fail without
  changing auction state or version.
- ✅ After a successful withdrawal, the bidder MAY submit a new bid at any
  valid amount while the auction remains `OPEN`.

### 3.4 Tie Breaking

If multiple active bidders share the highest bid amount, the earliest
accepted active bid MUST win.

Required behavior:

- ✅ Acceptance order MUST be assigned by the system.
- ✅ Acceptance order MUST be deterministic.
- ✅ Acceptance order MUST remain stable across replication.
- ✅ Acceptance order MUST remain stable across restart.
- ✅ Acceptance order MUST remain stable across failover.
- ✅ Replaced or withdrawn bids MUST NOT retain winner eligibility through
  their original acceptance order.

---

## 4. Visibility

### 4.1 Before Reveal

Before reveal, external users MAY see:

- auction metadata
- auction state
- distinct active bidder count

Before reveal, external users MUST NOT see:

- bid amounts
- bidder identities
- reserve price
- reserve status
- leading bid
- winning bidder
- winning amount

Required behavior:

- All externally visible pre-reveal responses MUST apply the same
  visibility restrictions.
- Live updates MUST NOT reveal information prohibited before reveal.
- Search and listing responses MUST NOT reveal information prohibited
  before reveal.
- Internal services MAY use hidden bid and reserve data only to enforce
  auction rules.

### 4.2 After Reveal

After reveal, external users MAY see:

- whether the reserve was met
- whether the auction produced a winner
- winning bidder
- winning amount
- final bidder count

After reveal, external users MUST NOT see:

- losing bidder identities
- losing bid amounts
- complete bid history

Required behavior:

- The published winner MUST be derived from the final active bid set.
- The published winning amount MUST be the winning bidder's final active
  bid amount.
- If the auction does not produce a winner, the system MUST NOT publish
  a winning bidder or winning amount.
- Post-reveal responses MUST continue to protect losing bid data.

---

## 5. Auction Outcome

An auction has a winner if and only if:

- at least one valid bid was accepted
- the highest accepted active bid meets or exceeds the reserve price

Possible outcomes:

- No bids received
- Reserve not met
- Successful sale

Required behavior:

- If no bids were received, the outcome MUST be `No bids received`.
- If at least one bid was received but the highest accepted active bid
  is below the reserve price, the outcome MUST be `Reserve not met`.
- If the highest accepted active bid meets or exceeds the reserve price,
  the outcome MUST be `Successful sale`.
- If the reserve is not met, no winner MUST be published.
- Outcome calculation MUST use only the final active bid set at reveal.

---

## 6. Optimistic Concurrency

Mutations use optimistic concurrency internally.

Required behavior:

- Each mutation MUST validate against an authoritative auction version.
- If a mutation encounters a stale version, the service MAY retry the
  logical request against the latest authoritative state.
- Each retry MUST revalidate all domain rules.
- Retries MUST be bounded.
- If retries are exhausted, the system MUST return a specific
  concurrency conflict response.
- A failed concurrency retry sequence MUST NOT partially apply the
  mutation.
- A successful retry sequence MUST apply the logical request at most
  once.

---

## 7. Idempotency

Every mutation request carries a unique request identifier.

Required behavior:

- Every mutation request MUST include a unique request identifier.
- The system MUST guarantee at-most-once application for a given request
  identifier.
- A repeated request with the same request identifier and same request
  contents MUST return the original result.
- A repeated request with the same request identifier MUST NOT apply the
  mutation again.
- Reusing a request identifier with different request contents MUST be
  rejected.
- Idempotency records MUST survive retry, replication, and failover for
  committed mutations.

---

## 8. Read Consistency

Authoritative reads use the current primary.

Authoritative reads include:

- single-auction status
- bidder count
- bidder's own active bid
- reveal status
- auction outcome
- reads used by mutations
- live auction updates

Required behavior:

- Authoritative reads MUST be served from the current primary or from a
  source proven to contain the same committed state.
- Reads used by mutations MUST observe the latest authoritative state
  required for rule validation.
- Live auction updates MUST reflect primary-committed state.
- Replica reads MAY be used only for stale-tolerant discovery
  operations.
- Search and listing operations MAY use stale-tolerant replica reads
  only if they do not violate visibility rules.

---

## 9. Write Acknowledgement

A mutation is acknowledged only after it has been committed to:

- the primary
- the designated synchronous backup

Required behavior:

- The system MUST NOT acknowledge a mutation before the primary commits
  it.
- The system MUST NOT acknowledge a mutation before the designated
  synchronous backup commits it.
- If the acknowledgement requirement cannot be satisfied, the mutation
  MUST NOT be committed.
- If the acknowledgement requirement cannot be satisfied, the auction
  version MUST NOT advance.
- If the acknowledgement requirement cannot be satisfied, the client
  MUST receive a failure response.
- An acknowledged mutation MUST be recoverable after primary failover.

---

## 10. Failover

### Promotion Eligibility

Required behavior:

- Only a backup known to contain the complete committed state MAY become
  primary.
- A backup with missing, stale, or unverified committed state MUST NOT
  become primary.
- Promotion eligibility MUST be determined using authoritative cluster
  coordination state.

### Promotion Barrier

Before accepting writes, a promoted primary MUST:

- receive the current epoch
- confirm complete committed state
- establish a synchronized backup
- complete promotion readiness

Required behavior:

- A promoted primary MUST NOT accept writes before completing the
  promotion barrier.
- Reads requiring authoritative state SHOULD be delayed or failed until
  promotion readiness is complete.

### Stale Primary Protection

Every primary assignment is associated with a monotonically increasing
epoch.

Required behavior:

- Each primary assignment MUST have an epoch.
- Epoch values MUST increase monotonically.
- Mutations from older epochs MUST be rejected.
- Replication requests from older epochs MUST be rejected.
- A former primary MUST synchronize before becoming eligible for
  promotion again.

### In-Flight Requests

Required behavior:

- Interrupted mutations MAY be retried only after the new primary is
  fully ready.
- Retries of interrupted mutations MUST preserve the original request
  identifier.
- Retried mutations MUST revalidate all domain rules under the new
  primary.
- A mutation whose result is unknown MUST be resolved through
  idempotency before it is retried.

### Overdue Auctions

Deadlines remain authoritative during failover.

After promotion, the new primary MAY finalize overdue auctions only
after validating:

- current epoch
- complete committed state
- synchronized backup
- overdue status
- idempotency

Required behavior:

- Failover MUST NOT extend or reset auction deadlines.
- Overdue auction finalization MUST obey the same reveal and visibility
  rules as request-initiated reveal.
- Overdue auction finalization MUST be idempotent.
