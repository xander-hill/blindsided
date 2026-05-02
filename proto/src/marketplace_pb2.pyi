from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class Item(_message.Message):
    __slots__ = ("item_id", "seller_id", "title", "category", "description", "starting_price", "current_price", "quantity", "status", "version")
    ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    SELLER_ID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    STARTING_PRICE_FIELD_NUMBER: _ClassVar[int]
    CURRENT_PRICE_FIELD_NUMBER: _ClassVar[int]
    QUANTITY_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    item_id: str
    seller_id: str
    title: str
    category: str
    description: str
    starting_price: float
    current_price: float
    quantity: int
    status: str
    version: int
    def __init__(self, item_id: _Optional[str] = ..., seller_id: _Optional[str] = ..., title: _Optional[str] = ..., category: _Optional[str] = ..., description: _Optional[str] = ..., starting_price: _Optional[float] = ..., current_price: _Optional[float] = ..., quantity: _Optional[int] = ..., status: _Optional[str] = ..., version: _Optional[int] = ...) -> None: ...

class ClusterInfoRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ClusterInfoResponse(_message.Message):
    __slots__ = ("success", "node_addresses")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    NODE_ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    success: bool
    node_addresses: _containers.RepeatedScalarFieldContainer[str]
    def __init__(self, success: bool = ..., node_addresses: _Optional[_Iterable[str]] = ...) -> None: ...

class CreateItemRequest(_message.Message):
    __slots__ = ("item",)
    ITEM_FIELD_NUMBER: _ClassVar[int]
    item: Item
    def __init__(self, item: _Optional[_Union[Item, _Mapping]] = ...) -> None: ...

class CreateItemResponse(_message.Message):
    __slots__ = ("ok", "item_id", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    item_id: str
    message: str
    def __init__(self, ok: bool = ..., item_id: _Optional[str] = ..., message: _Optional[str] = ...) -> None: ...

class GetItemRequest(_message.Message):
    __slots__ = ("item_id",)
    ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    item_id: str
    def __init__(self, item_id: _Optional[str] = ...) -> None: ...

class GetItemResponse(_message.Message):
    __slots__ = ("ok", "item", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    ITEM_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    item: Item
    message: str
    def __init__(self, ok: bool = ..., item: _Optional[_Union[Item, _Mapping]] = ..., message: _Optional[str] = ...) -> None: ...

class SearchRequest(_message.Message):
    __slots__ = ("query", "category")
    QUERY_FIELD_NUMBER: _ClassVar[int]
    CATEGORY_FIELD_NUMBER: _ClassVar[int]
    query: str
    category: str
    def __init__(self, query: _Optional[str] = ..., category: _Optional[str] = ...) -> None: ...

class SearchResponse(_message.Message):
    __slots__ = ("ok", "items", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    items: _containers.RepeatedCompositeFieldContainer[Item]
    message: str
    def __init__(self, ok: bool = ..., items: _Optional[_Iterable[_Union[Item, _Mapping]]] = ..., message: _Optional[str] = ...) -> None: ...

class UpdateItemRequest(_message.Message):
    __slots__ = ("item_id", "seller_id", "description", "quantity", "status", "expected_version")
    ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    SELLER_ID_FIELD_NUMBER: _ClassVar[int]
    DESCRIPTION_FIELD_NUMBER: _ClassVar[int]
    QUANTITY_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    item_id: str
    seller_id: str
    description: str
    quantity: int
    status: str
    expected_version: int
    def __init__(self, item_id: _Optional[str] = ..., seller_id: _Optional[str] = ..., description: _Optional[str] = ..., quantity: _Optional[int] = ..., status: _Optional[str] = ..., expected_version: _Optional[int] = ...) -> None: ...

class UpdateItemResponse(_message.Message):
    __slots__ = ("ok", "new_version", "message")
    OK_FIELD_NUMBER: _ClassVar[int]
    NEW_VERSION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    new_version: int
    message: str
    def __init__(self, ok: bool = ..., new_version: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class BidRequest(_message.Message):
    __slots__ = ("item_id", "buyer_id", "amount", "expected_version")
    ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    BUYER_ID_FIELD_NUMBER: _ClassVar[int]
    AMOUNT_FIELD_NUMBER: _ClassVar[int]
    EXPECTED_VERSION_FIELD_NUMBER: _ClassVar[int]
    item_id: str
    buyer_id: str
    amount: float
    expected_version: int
    def __init__(self, item_id: _Optional[str] = ..., buyer_id: _Optional[str] = ..., amount: _Optional[float] = ..., expected_version: _Optional[int] = ...) -> None: ...

class BidResponse(_message.Message):
    __slots__ = ("success", "current_price", "new_version", "message")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    CURRENT_PRICE_FIELD_NUMBER: _ClassVar[int]
    NEW_VERSION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    success: bool
    current_price: float
    new_version: int
    message: str
    def __init__(self, success: bool = ..., current_price: _Optional[float] = ..., new_version: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class AuctionRequest(_message.Message):
    __slots__ = ("item_id", "user_id")
    ITEM_ID_FIELD_NUMBER: _ClassVar[int]
    USER_ID_FIELD_NUMBER: _ClassVar[int]
    item_id: str
    user_id: str
    def __init__(self, item_id: _Optional[str] = ..., user_id: _Optional[str] = ...) -> None: ...

class AuctionUpdate(_message.Message):
    __slots__ = ("current_price", "highest_bidder_id", "version", "message")
    CURRENT_PRICE_FIELD_NUMBER: _ClassVar[int]
    HIGHEST_BIDDER_ID_FIELD_NUMBER: _ClassVar[int]
    VERSION_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_FIELD_NUMBER: _ClassVar[int]
    current_price: float
    highest_bidder_id: str
    version: int
    message: str
    def __init__(self, current_price: _Optional[float] = ..., highest_bidder_id: _Optional[str] = ..., version: _Optional[int] = ..., message: _Optional[str] = ...) -> None: ...

class PutRequest(_message.Message):
    __slots__ = ("item", "is_update", "skip_consistency_check")
    ITEM_FIELD_NUMBER: _ClassVar[int]
    IS_UPDATE_FIELD_NUMBER: _ClassVar[int]
    SKIP_CONSISTENCY_CHECK_FIELD_NUMBER: _ClassVar[int]
    item: Item
    is_update: bool
    skip_consistency_check: bool
    def __init__(self, item: _Optional[_Union[Item, _Mapping]] = ..., is_update: bool = ..., skip_consistency_check: bool = ...) -> None: ...

class PutResponse(_message.Message):
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
    __slots__ = ("ok", "items", "items_found")
    OK_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FOUND_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    items: _containers.RepeatedCompositeFieldContainer[Item]
    items_found: int
    def __init__(self, ok: bool = ..., items: _Optional[_Iterable[_Union[Item, _Mapping]]] = ..., items_found: _Optional[int] = ...) -> None: ...

class StateRequest(_message.Message):
    __slots__ = ("requester_id",)
    REQUESTER_ID_FIELD_NUMBER: _ClassVar[int]
    requester_id: str
    def __init__(self, requester_id: _Optional[str] = ...) -> None: ...

class StateResponse(_message.Message):
    __slots__ = ("ok", "items", "last_included_version")
    OK_FIELD_NUMBER: _ClassVar[int]
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    LAST_INCLUDED_VERSION_FIELD_NUMBER: _ClassVar[int]
    ok: bool
    items: _containers.RepeatedCompositeFieldContainer[Item]
    last_included_version: int
    def __init__(self, ok: bool = ..., items: _Optional[_Iterable[_Union[Item, _Mapping]]] = ..., last_included_version: _Optional[int] = ...) -> None: ...

class ReplicationRequest(_message.Message):
    __slots__ = ("item", "primary_id")
    ITEM_FIELD_NUMBER: _ClassVar[int]
    PRIMARY_ID_FIELD_NUMBER: _ClassVar[int]
    item: Item
    primary_id: str
    def __init__(self, item: _Optional[_Union[Item, _Mapping]] = ..., primary_id: _Optional[str] = ...) -> None: ...

class ReplicationResponse(_message.Message):
    __slots__ = ("success", "ack_version")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    ACK_VERSION_FIELD_NUMBER: _ClassVar[int]
    success: bool
    ack_version: int
    def __init__(self, success: bool = ..., ack_version: _Optional[int] = ...) -> None: ...

class HealthCheckRequest(_message.Message):
    __slots__ = ("request_source",)
    REQUEST_SOURCE_FIELD_NUMBER: _ClassVar[int]
    request_source: str
    def __init__(self, request_source: _Optional[str] = ...) -> None: ...

class HealthCheckResponse(_message.Message):
    __slots__ = ("alive", "item_count", "role")
    ALIVE_FIELD_NUMBER: _ClassVar[int]
    ITEM_COUNT_FIELD_NUMBER: _ClassVar[int]
    ROLE_FIELD_NUMBER: _ClassVar[int]
    alive: bool
    item_count: int
    role: str
    def __init__(self, alive: bool = ..., item_count: _Optional[int] = ..., role: _Optional[str] = ...) -> None: ...

class PromotionRequest(_message.Message):
    __slots__ = ("new_role",)
    NEW_ROLE_FIELD_NUMBER: _ClassVar[int]
    new_role: str
    def __init__(self, new_role: _Optional[str] = ...) -> None: ...

class PromotionResponse(_message.Message):
    __slots__ = ("success",)
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    success: bool
    def __init__(self, success: bool = ...) -> None: ...

class Empty(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class SnapshotResponse(_message.Message):
    __slots__ = ("items",)
    ITEMS_FIELD_NUMBER: _ClassVar[int]
    items: _containers.RepeatedCompositeFieldContainer[Item]
    def __init__(self, items: _Optional[_Iterable[_Union[Item, _Mapping]]] = ...) -> None: ...

class RegisterRequest(_message.Message):
    __slots__ = ("address",)
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    address: str
    def __init__(self, address: _Optional[str] = ...) -> None: ...

class RegisterResponse(_message.Message):
    __slots__ = ("success", "is_primary")
    SUCCESS_FIELD_NUMBER: _ClassVar[int]
    IS_PRIMARY_FIELD_NUMBER: _ClassVar[int]
    success: bool
    is_primary: bool
    def __init__(self, success: bool = ..., is_primary: bool = ...) -> None: ...

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
