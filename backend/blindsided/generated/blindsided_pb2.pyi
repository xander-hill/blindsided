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
AUCTION_STATE_UNSPECIFIED: AuctionState
AUCTION_STATE_OPEN: AuctionState
AUCTION_STATE_REVEALED: AuctionState

class Auction(_message.Message):
    __slots__ = ("auction_id", "seller_id", "title", "category", "description", "reserve_price", "bids", "state", "version", "reserve_met")
    class BidsEntry(_message.Message):
        __slots__ = ("key", "value")
        KEY_FIELD_NUMBER: _ClassVar[int]
        VALUE_FIELD_NUMBER: _ClassVar[int]
        key: str
        value: float
        def __init__(self, key: _Optional[str] = ..., value: _Optional[float] = ...) -> None: ...
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    SELLER_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    RESERVE_PRICE_FIELD_NUMBER: _ClassVar[int]
    BIDS_FIELD_NUMBER: _ClassVar[int]
    STATE_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    RESERVE_MET_FIELD_NUMBER: _ClassVar[int]
    auction_id: str
    seller_id: str
    title: str
    category: str
    description: str
    reserve_price: float
    bids: _containers.ScalarMap[str, float]
    state: AuctionState
    version: int
    reserve_met: bool
    def __init__(self, auction_id: _Optional[str] = ..., seller_id: _Optional[str] = ..., title: _Optional[str] = ..., category: _Optional[str] = ..., description: _Optional[str] = ..., reserve_price: _Optional[float] = ..., bids: _Optional[_Mapping[str, float]] = ..., state: _Optional[_Union[AuctionState, str]] = ..., version: _Optional[int] = ..., reserve_met: bool = ...) -> None: ...

class CreateAuctionRequest(_message.Message):
    __slots__ = ("auction",)
    AUCTION_FIELD_NUMBER: _ClassVar[int]
    auction: Auction
    def __init__(self, auction: _Optional[_Union[Auction, _Mapping]] = ...) -> None: ...

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
    __slots__ = ("ok", "auctions", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    AUCTIONS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    auctions: _containers.RepeatedCompositeFieldContainer[Auction]
    message: str
    def __init__(self, ok: bool = ..., auctions: _Optional[_Iterable[_Union[Auction, _Mapping]]] = ..., message: _Optional[str] = ...) -> None: ...

class RevealAuctionRequest(_message.Message):
    __slots__ = ("auction_id", "seller_id", "expected_version")
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    SELLER_ID_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    auction_id: str
    seller_id: str
    expected_version: int
    def __init__(self, auction_id: _Optional[str] = ..., seller_id: _Optional[str] = ..., expected_version: _Optional[int] = ...) -> None: ...

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
    __slots__ = ("auction_id", "bidder_id", "amount", "expected_version")
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    BIDDER_ID_FIELD_NUMBER: _ClassVar[int]
    AMOUNT_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    auction_id: str
    bidder_id: str
    amount: float
    expected_version: int
    def __init__(self, auction_id: _Optional[str] = ..., bidder_id: _Optional[str] = ..., amount: _Optional[float] = ..., expected_version: _Optional[int] = ...) -> None: ...

class BidResponse(_message.Message):
    __slots__ = ("success", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    message: str
    def __init__(self, success: bool = ..., message: _Optional[str] = ...) -> None: ...

class AuctionRequest(_message.Message):
    __slots__ = ("auction_id", "user_id")
    AUCTION_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    auction_id: str
    user_id: str
    def __init__(self, auction_id: _Optional[str] = ..., user_id: _Optional[str] = ...) -> None: ...

class AuctionUpdate(_message.Message):
    __slots__ = ("state", "message", "high_range", "low_range", "bidder_count", "reserve_met", "winning_amount", "winning_bidder_id")
    STATE_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    HIGH_RANGE_FIELD_NUMBER: _ClassVar[int]
    LOW_RANGE_FIELD_NUMBER: _ClassVar[int]
    BIDDER_COUNT_FIELD_NUMBER: _ClassVar[int]
    RESERVE_MET_FIELD_NUMBER: _ClassVar[int]
    WINNING_AMOUNT_FIELD_NUMBER: _ClassVar[int]
    WINNING_BIDDER_ID_FIELD_NUMBER: _ClassVar[int]
    state: AuctionState
    message: str
    high_range: float
    low_range: float
    bidder_count: int
    reserve_met: bool
    winning_amount: float
    winning_bidder_id: str
    def __init__(self, state: _Optional[_Union[AuctionState, str]] = ..., message: _Optional[str] = ..., high_range: _Optional[float] = ..., low_range: _Optional[float] = ..., bidder_count: _Optional[int] = ..., reserve_met: bool = ..., winning_amount: _Optional[float] = ..., winning_bidder_id: _Optional[str] = ...) -> None: ...

class CommitRequest(_message.Message):
    __slots__ = ("auction", "is_reveal_event", "skip_consistency_check")
    AUCTION_FIELD_NUMBER: _ClassVar[int]
    IS_REVEAL_EVENT_FIELD_NUMBER: _ClassVar[int]
    SKIP_CONSISTENCY_CHECK_FIELD_NUMBER: _ClassVar[int]
    auction: Auction
    is_reveal_event: bool
    skip_consistency_check: bool
    def __init__(self, auction: _Optional[_Union[Auction, _Mapping]] = ..., is_reveal_event: bool = ..., skip_consistency_check: bool = ...) -> None: ...

class CommitResponse(_message.Message):
    __slots__ = ("success", "current_version", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_VERSION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    current_version: int
    message: str
    def __init__(self, success: bool = ..., current_version: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class QueryRequest(_message.Message):
    __slots__ = ("filter",)
    FILTER_FIELD_NUMBER: _ClassVar[int]
    filter: str
    def __init__(self, filter: _Optional[str] = ...) -> None: ...

class QueryResponse(_message.Message):
    __slots__ = ("ok", "auctions", "count", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    AUCTIONS_FIELD_NUMBER: _ClassVar[int]
    COUNT_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    auctions: _containers.RepeatedCompositeFieldContainer[Auction]
    count: int
    message: str
    def __init__(self, ok: bool = ..., auctions: _Optional[_Iterable[_Union[Auction, _Mapping]]] = ..., count: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class StateRequest(_message.Message):
    __slots__ = ("requester_id",)
    REQUESTER_ID_FIELD_NUMBER: _ClassVar[int]
    requester_id: str
    def __init__(self, requester_id: _Optional[str] = ...) -> None: ...

class StateResponse(_message.Message):
    __slots__ = ("ok", "auctions", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    AUCTIONS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    auctions: _containers.RepeatedCompositeFieldContainer[Auction]
    message: str
    def __init__(self, ok: bool = ..., auctions: _Optional[_Iterable[_Union[Auction, _Mapping]]] = ..., message: _Optional[str] = ...) -> None: ...

class ReplicationRequest(_message.Message):
    __slots__ = ("auction", "primary_id")
    AUCTION_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_ID_FIELD_NUMBER: _ClassVar[int]
    auction: Auction
    primary_id: str
    def __init__(self, auction: _Optional[_Union[Auction, _Mapping]] = ..., primary_id: _Optional[str] = ...) -> None: ...

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
