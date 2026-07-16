import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class AuctionState(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AUCTION_STATE_UNSPECIFIED: _ClassVar[AuctionState]
    AUCTION_STATE_OPEN: _ClassVar[AuctionState]
    AUCTION_STATE_REVEALED: _ClassVar[AuctionState]

class AuctionMutationType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AUCTION_MUTATION_TYPE_UNSPECIFIED: _ClassVar[AuctionMutationType]
    AUCTION_MUTATION_TYPE_CREATE: _ClassVar[AuctionMutationType]
    AUCTION_MUTATION_TYPE_PLACE_BID: _ClassVar[AuctionMutationType]
    AUCTION_MUTATION_TYPE_WITHDRAW_BID: _ClassVar[AuctionMutationType]
    AUCTION_MUTATION_TYPE_REVEAL: _ClassVar[AuctionMutationType]

class AuctionOutcome(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    AUCTION_OUTCOME_UNSPECIFIED: _ClassVar[AuctionOutcome]
    AUCTION_OUTCOME_NO_BIDS: _ClassVar[AuctionOutcome]
    AUCTION_OUTCOME_RESERVE_NOT_MET: _ClassVar[AuctionOutcome]
    AUCTION_OUTCOME_SUCCESSFUL_SALE: _ClassVar[AuctionOutcome]

class MutationFailureReason(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    MUTATION_FAILURE_REASON_UNSPECIFIED: _ClassVar[MutationFailureReason]
    MUTATION_FAILURE_REASON_NOT_FOUND: _ClassVar[MutationFailureReason]
    MUTATION_FAILURE_REASON_INVALID_STATE: _ClassVar[MutationFailureReason]
    MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT: _ClassVar[MutationFailureReason]
    MUTATION_FAILURE_REASON_REPLICATION_FAILED: _ClassVar[MutationFailureReason]
    MUTATION_FAILURE_REASON_IDEMPOTENCY_CONFLICT: _ClassVar[MutationFailureReason]
AUCTION_STATE_UNSPECIFIED: AuctionState
AUCTION_STATE_OPEN: AuctionState
AUCTION_STATE_REVEALED: AuctionState
AUCTION_MUTATION_TYPE_UNSPECIFIED: AuctionMutationType
AUCTION_MUTATION_TYPE_CREATE: AuctionMutationType
AUCTION_MUTATION_TYPE_PLACE_BID: AuctionMutationType
AUCTION_MUTATION_TYPE_WITHDRAW_BID: AuctionMutationType
AUCTION_MUTATION_TYPE_REVEAL: AuctionMutationType
AUCTION_OUTCOME_UNSPECIFIED: AuctionOutcome
AUCTION_OUTCOME_NO_BIDS: AuctionOutcome
AUCTION_OUTCOME_RESERVE_NOT_MET: AuctionOutcome
AUCTION_OUTCOME_SUCCESSFUL_SALE: AuctionOutcome
MUTATION_FAILURE_REASON_UNSPECIFIED: MutationFailureReason
MUTATION_FAILURE_REASON_NOT_FOUND: MutationFailureReason
MUTATION_FAILURE_REASON_INVALID_STATE: MutationFailureReason
MUTATION_FAILURE_REASON_CONCURRENCY_CONFLICT: MutationFailureReason
MUTATION_FAILURE_REASON_REPLICATION_FAILED: MutationFailureReason
MUTATION_FAILURE_REASON_IDEMPOTENCY_CONFLICT: MutationFailureReason

class Auction(_message.Message):
    __slots__ = ("auction_id", "seller_id", "title", "category", "description", "reserve_price", "bids", "state", "version", "ends_at", "next_bid_sequence", "result")
    class BidsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: ActiveBid
        def __init__(self, key: _Optional[str] = ..., value: _Optional[_Union[ActiveBid, _Mapping]] = ...) -> None: ...
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    SELLER_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    RESERVE_PRICE_FIELD_NUMBER: _ClassVar[int]
    BIDS_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    ENDS_AT_FIELD_NUMBER: _ClassVar[int]
    NEXT_BID_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    auction_id: str
    seller_id: str
    title: str
    category: str
    description: str
    reserve_price: float
    bids: _containers.MessageMap[str, ActiveBid]
    state: AuctionState
    version: int
    ends_at: _timestamp_pb2.Timestamp
    next_bid_sequence: int
    result: AuctionResult
    def __init__(self, auction_id: _Optional[str] = ..., seller_id: _Optional[str] = ..., title: _Optional[str] = ..., category: _Optional[str] = ..., description: _Optional[str] = ..., reserve_price: _Optional[float] = ..., bids: _Optional[_Mapping[str, ActiveBid]] = ..., state: _Optional[_Union[AuctionState, str]] = ..., version: _Optional[int] = ..., ends_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., next_bid_sequence: _Optional[int] = ..., result: _Optional[_Union[AuctionResult, _Mapping]] = ...) -> None: ...

class PublicAuction(_message.Message):
    __slots__ = ("auction_id", "seller_id", "title", "category", "description", "state", "ends_at", "bidder_count", "result")
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    SELLER_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    ENDS_AT_FIELD_NUMBER: _ClassVar[int]
    BIDDER_COUNT_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    auction_id: str
    seller_id: str
    title: str
    category: str
    description: str
    state: AuctionState
    ends_at: _timestamp_pb2.Timestamp
    bidder_count: int
    result: AuctionResult
    def __init__(self, auction_id: _Optional[str] = ..., seller_id: _Optional[str] = ..., title: _Optional[str] = ..., category: _Optional[str] = ..., description: _Optional[str] = ..., state: _Optional[_Union[AuctionState, str]] = ..., ends_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., bidder_count: _Optional[int] = ..., result: _Optional[_Union[AuctionResult, _Mapping]] = ...) -> None: ...

class AuctionResult(_message.Message):
    __slots__ = ("outcome", "reserve_met", "has_winner", "winning_bidder_id", "winning_amount")
    OUTCOME_FIELD_NUMBER: _ClassVar[int]
    RESERVE_MET_FIELD_NUMBER: _ClassVar[int]
    HAS_WINNER_FIELD_NUMBER: _ClassVar[int]
    WINNING_BIDDER_ID_FIELD_NUMBER: _ClassVar[int]
    WINNING_AMOUNT_FIELD_NUMBER: _ClassVar[int]
    outcome: AuctionOutcome
    reserve_met: bool
    has_winner: bool
    winning_bidder_id: str
    winning_amount: float
    def __init__(self, outcome: _Optional[_Union[AuctionOutcome, str]] = ..., reserve_met: bool = ..., has_winner: bool = ..., winning_bidder_id: _Optional[str] = ..., winning_amount: _Optional[float] = ...) -> None: ...

class CreateAuctionRequest(_message.Message):
    __slots__ = ("seller_id", "title", "category", "description", "reserve_price", "ends_at", "request_id")
    SELLER_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    RESERVE_PRICE_FIELD_NUMBER: _ClassVar[int]
    ENDS_AT_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    seller_id: str
    title: str
    category: str
    description: str
    reserve_price: float
    ends_at: _timestamp_pb2.Timestamp
    request_id: str
    def __init__(self, seller_id: _Optional[str] = ..., title: _Optional[str] = ..., category: _Optional[str] = ..., description: _Optional[str] = ..., reserve_price: _Optional[float] = ..., ends_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., request_id: _Optional[str] = ...) -> None: ...

class CreateAuctionResponse(_message.Message):
    __slots__ = ("ok", "auction_id", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    auction_id: str
    message: str
    def __init__(self, ok: bool = ..., auction_id: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class GetAuctionRequest(_message.Message):
    __slots__ = ("auction_id",)
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    auction_id: str
    def __init__(self, auction_id: _Optional[str] = ...) -> None: ...

class GetAuctionResponse(_message.Message):
    __slots__ = ("ok", "auction", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    AUCTION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    auction: PublicAuction
    message: str
    def __init__(self, ok: bool = ..., auction: _Optional[_Union[PublicAuction, _Mapping]] = ..., message: _Optional[str] = ...) -> None: ...

class GetStoredAuctionResponse(_message.Message):
    __slots__ = ("ok", "auction", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    AUCTION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    auction: Auction
    message: str
    def __init__(self, ok: bool = ..., auction: _Optional[_Union[Auction, _Mapping]] = ..., message: _Optional[str] = ...) -> None: ...

class SearchAuctionsRequest(_message.Message):
    __slots__ = ("query", "category")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    query: str
    category: str
    def __init__(self, query: _Optional[str] = ..., category: _Optional[str] = ...) -> None: ...

class SearchAuctionsResponse(_message.Message):
    __slots__ = ("ok", "auctions", "message", "count")
    OK_FIELD_NUMBER: _ClassVar[int]
    AUCTIONS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    auctions: _containers.RepeatedCompositeFieldContainer[PublicAuction]
    message: str
    count: int
    def __init__(self, ok: bool = ..., auctions: _Optional[_Iterable[_Union[PublicAuction, _Mapping]]] = ..., message: _Optional[str] = ..., count: _Optional[int] = ...) -> None: ...

class GetStoredAuctionsResponse(_message.Message):
    __slots__ = ("ok", "auctions", "message", "count")
    OK_FIELD_NUMBER: _ClassVar[int]
    AUCTIONS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    auctions: _containers.RepeatedCompositeFieldContainer[Auction]
    message: str
    count: int
    def __init__(self, ok: bool = ..., auctions: _Optional[_Iterable[_Union[Auction, _Mapping]]] = ..., message: _Optional[str] = ..., count: _Optional[int] = ...) -> None: ...

class RevealAuctionRequest(_message.Message):
    __slots__ = ("auction_id", "seller_id", "expected_version", "request_id")
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    SELLER_ID_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    auction_id: str
    seller_id: str
    expected_version: int
    request_id: str
    def __init__(self, auction_id: _Optional[str] = ..., seller_id: _Optional[str] = ..., expected_version: _Optional[int] = ..., request_id: _Optional[str] = ...) -> None: ...

class RevealAuctionResponse(_message.Message):
    __slots__ = ("ok", "final_version", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    FINAL_VERSION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    final_version: int
    message: str
    def __init__(self, ok: bool = ..., final_version: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class BidRequest(_message.Message):
    __slots__ = ("auction_id", "bidder_id", "amount", "expected_version", "request_id")
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    BIDDER_ID_FIELD_NUMBER: _ClassVar[int]
    AMOUNT_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    auction_id: str
    bidder_id: str
    amount: float
    expected_version: int
    request_id: str
    def __init__(self, auction_id: _Optional[str] = ..., bidder_id: _Optional[str] = ..., amount: _Optional[float] = ..., expected_version: _Optional[int] = ..., request_id: _Optional[str] = ...) -> None: ...

class BidResponse(_message.Message):
    __slots__ = ("success", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    message: str
    def __init__(self, success: bool = ..., message: _Optional[str] = ...) -> None: ...

class WithdrawBidRequest(_message.Message):
    __slots__ = ("auction_id", "bidder_id", "expected_version", "request_id")
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    BIDDER_ID_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    auction_id: str
    bidder_id: str
    expected_version: int
    request_id: str
    def __init__(self, auction_id: _Optional[str] = ..., bidder_id: _Optional[str] = ..., expected_version: _Optional[int] = ..., request_id: _Optional[str] = ...) -> None: ...

class WithdrawBidResponse(_message.Message):
    __slots__ = ("success", "final_version", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    FINAL_VERSION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    final_version: int
    message: str
    def __init__(self, success: bool = ..., final_version: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class AuctionRequest(_message.Message):
    __slots__ = ("auction_id", "user_id")
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    auction_id: str
    user_id: str
    def __init__(self, auction_id: _Optional[str] = ..., user_id: _Optional[str] = ...) -> None: ...

class AuctionUpdate(_message.Message):
    __slots__ = ("state", "message", "bidder_count", "version", "result")
    STATE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    BIDDER_COUNT_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    RESULT_FIELD_NUMBER: _ClassVar[int]
    state: AuctionState
    message: str
    bidder_count: int
    version: int
    result: AuctionResult
    def __init__(self, state: _Optional[_Union[AuctionState, str]] = ..., message: _Optional[str] = ..., bidder_count: _Optional[int] = ..., version: _Optional[int] = ..., result: _Optional[_Union[AuctionResult, _Mapping]] = ...) -> None: ...

class AuctionMutationRequest(_message.Message):
    __slots__ = ("mutation_type", "auction", "bidder_id", "expected_version", "request_id")
    MUTATION_TYPE_FIELD_NUMBER: _ClassVar[int]
    AUCTION_FIELD_NUMBER: _ClassVar[int]
    BIDDER_ID_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    mutation_type: AuctionMutationType
    auction: Auction
    bidder_id: str
    expected_version: int
    request_id: str
    def __init__(self, mutation_type: _Optional[_Union[AuctionMutationType, str]] = ..., auction: _Optional[_Union[Auction, _Mapping]] = ..., bidder_id: _Optional[str] = ..., expected_version: _Optional[int] = ..., request_id: _Optional[str] = ...) -> None: ...

class AuctionMutationResponse(_message.Message):
    __slots__ = ("success", "current_version", "message", "failure_reason", "auction_id", "replayed")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    FAILURE_REASON_FIELD_NUMBER: _ClassVar[int]
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    REPLAYED_FIELD_NUMBER: _ClassVar[int]
    success: bool
    current_version: int
    message: str
    failure_reason: MutationFailureReason
    auction_id: str
    replayed: bool
    def __init__(self, success: bool = ..., current_version: _Optional[int] = ..., message: _Optional[str] = ..., failure_reason: _Optional[_Union[MutationFailureReason, str]] = ..., auction_id: _Optional[str] = ..., replayed: bool = ...) -> None: ...

class StateRequest(_message.Message):
    __slots__ = ("requester_id",)
    REQUESTER_ID_FIELD_NUMBER: _ClassVar[int]
    requester_id: str
    def __init__(self, requester_id: _Optional[str] = ...) -> None: ...

class StateResponse(_message.Message):
    __slots__ = ("ok", "auctions", "message", "idempotency_records")
    OK_FIELD_NUMBER: _ClassVar[int]
    AUCTIONS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_RECORDS_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    auctions: _containers.RepeatedCompositeFieldContainer[Auction]
    message: str
    idempotency_records: _containers.RepeatedCompositeFieldContainer[IdempotencyRecord]
    def __init__(self, ok: bool = ..., auctions: _Optional[_Iterable[_Union[Auction, _Mapping]]] = ..., message: _Optional[str] = ..., idempotency_records: _Optional[_Iterable[_Union[IdempotencyRecord, _Mapping]]] = ...) -> None: ...

class ReplicationRequest(_message.Message):
    __slots__ = ("auction", "primary_id", "idempotency_record")
    AUCTION_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_ID_FIELD_NUMBER: _ClassVar[int]
    IDEMPOTENCY_RECORD_FIELD_NUMBER: _ClassVar[int]
    auction: Auction
    primary_id: str
    idempotency_record: IdempotencyRecord
    def __init__(self, auction: _Optional[_Union[Auction, _Mapping]] = ..., primary_id: _Optional[str] = ..., idempotency_record: _Optional[_Union[IdempotencyRecord, _Mapping]] = ...) -> None: ...

class ReplicationResponse(_message.Message):
    __slots__ = ("success", "ack_version", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ACK_VERSION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    ack_version: int
    message: str
    def __init__(self, success: bool = ..., ack_version: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class HealthCheckRequest(_message.Message):
    __slots__ = ("request_source",)
    REQUEST_SOURCE_FIELD_NUMBER: _ClassVar[int]
    request_source: str
    def __init__(self, request_source: _Optional[str] = ...) -> None: ...

class HealthCheckResponse(_message.Message):
    __slots__ = ("alive", "role", "message")
    ALIVE_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    alive: bool
    role: str
    message: str
    def __init__(self, alive: bool = ..., role: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class PromotionRequest(_message.Message):
    __slots__ = ("new_role",)
    NEW_ROLE_FIELD_NUMBER: _ClassVar[int]
    new_role: str
    def __init__(self, new_role: _Optional[str] = ...) -> None: ...

class PromotionResponse(_message.Message):
    __slots__ = ("success", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    message: str
    def __init__(self, success: bool = ..., message: _Optional[str] = ...) -> None: ...

class RegisterRequest(_message.Message):
    __slots__ = ("address",)
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    address: str
    def __init__(self, address: _Optional[str] = ...) -> None: ...

class RegisterResponse(_message.Message):
    __slots__ = ("success", "is_primary", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    IS_PRIMARY_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    is_primary: bool
    message: str
    def __init__(self, success: bool = ..., is_primary: bool = ..., message: _Optional[str] = ...) -> None: ...

class GetPrimaryRequest(_message.Message):
    __slots__ = ("requester_id",)
    REQUESTER_ID_FIELD_NUMBER: _ClassVar[int]
    requester_id: str
    def __init__(self, requester_id: _Optional[str] = ...) -> None: ...

class GetPrimaryResponse(_message.Message):
    __slots__ = ("success", "primary_address", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    primary_address: str
    message: str
    def __init__(self, success: bool = ..., primary_address: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class ClusterInfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ClusterInfoResponse(_message.Message):
    __slots__ = ("success", "node_addresses", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    NODE_ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    node_addresses: _containers.RepeatedScalarFieldContainer[str]
    message: str
    def __init__(self, success: bool = ..., node_addresses: _Optional[_Iterable[str]] = ..., message: _Optional[str] = ...) -> None: ...

class ActiveBid(_message.Message):
    __slots__ = ("amount", "acceptance_order")
    AMOUNT_FIELD_NUMBER: _ClassVar[int]
    ACCEPTANCE_ORDER_FIELD_NUMBER: _ClassVar[int]
    amount: float
    acceptance_order: int
    def __init__(self, amount: _Optional[float] = ..., acceptance_order: _Optional[int] = ...) -> None: ...

class IdempotencyRecord(_message.Message):
    __slots__ = ("request_id", "request_fingerprint", "response")
    REQUEST_ID_FIELD_NUMBER: _ClassVar[int]
    REQUEST_FINGERPRINT_FIELD_NUMBER: _ClassVar[int]
    RESPONSE_FIELD_NUMBER: _ClassVar[int]
    request_id: str
    request_fingerprint: bytes
    response: AuctionMutationResponse
    def __init__(self, request_id: _Optional[str] = ..., request_fingerprint: _Optional[bytes] = ..., response: _Optional[_Union[AuctionMutationResponse, _Mapping]] = ...) -> None: ...
