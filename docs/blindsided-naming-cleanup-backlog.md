# Blindsided Naming Cleanup Backlog

> Purpose: Track naming improvements separately from behavioral changes.
> These are recommendations, **not** a single refactor. Apply only when
> working in the relevant area of the codebase.

## Proto

### Services

Current Recommended

---

`BlindSided` `AuctionService`
`JudgeNode` `StorageReplicaService`
`Controller` `ClusterController`

### RPCs

Current Recommended

---

`OpenAuction` `CreateAuction`
`GetStatus` `GetAuction`
`PlaceSecretBid` `PlaceBid`
`DropTheGavel` `RevealAuction`
`JoinLiveAuction` `WatchAuction`

### Messages

Current Recommended

---

`OpenRequest` `CreateAuctionRequest`
`OpenResponse` `CreateAuctionResponse`
`StatusRequest` `GetAuctionRequest`
`StatusResponse` `GetAuctionResponse`
`SearchRequest` `SearchAuctionsRequest`
`SearchResponse` `SearchAuctionsResponse`
`GavelRequest` `RevealAuctionRequest`
`GavelResponse` `RevealAuctionResponse`

### Fields

Current Recommended

---

`is_revealed` `state`
`buyer_id` `bidder_id`
`winner_id` `winning_bidder_id`
`final_price` `winning_amount`
`reserve_status` `reserve_met`

---

## Controller

### Internal Methods

Current Recommended

---

`HeartbeatMonitor` `_monitor_heartbeats`
`ElectNewPrimary` `_elect_new_primary`
`NotifyPromotion` `_notify_promotion`

### Variables

Current Recommended

---

`addr` `node_address`
`new_primary` `new_primary_address`
`e` `error`

---

## Storage

### Class

Current Recommended

---

`JudgeNode` `StorageReplicaService`

### Members

Current Recommended

---

`vault` `auction_store`
`cv` `state_lock`
`role` `replica_role`
`my_full_address` `node_address`

### RPCs / Methods

Current Recommended

---

`CommitToVault` `ApplyAuctionMutation` _(attempted)_
`QueryVault` `GetAuction` / `SearchAuctions` _(attempted split)_
`ReplicateSecret` `ReplicateAuction`
`_sync_vault` `_synchronize_from_primary`

### Variables

---

Current Recommended

---

`existing` `existing_auction`

`incoming` `incoming_auction` _(or `mutation`
after contract redesign)_

`max_bid` `highest_bid_amount`

`addr` `peer_address`

`resp` descriptive response name

`e` `error`

---

---

## Auction Service

### Class

Current Recommended

---

`BlindSidedService` `AuctionService`

### Methods

Current Recommended

---

`_judge_stub` `_create_storage_stub`
`_get_all_judge_addresses` `_get_storage_node_addresses`
`_mask_for_fog` `_to_public_auction`
`_mask_for_opaque_fog` `_to_public_auction_update`

### Variables

Current Recommended

---

`primary` `primary_address`
`res` descriptive response name
`q_res` `query_response`
`status_res` `query_response`
`current_v` `current_version`
`reveal_state` `reveal_mutation`
`bid_state` `bid_mutation`
`masked` `public_auction`
`prices` `bid_amounts`
`e` `error`
