"""Public API for the standalone vehicle-composition catalogue and weighbridge
fact store.

    from weighbridge import Catalog, Warehouse

两个模块互相独立：``Catalog`` 不知道磅单；``Warehouse`` 只通过
``Product``/``Snapshot`` 公开结构读取目录，也可导入外部同格式快照。
"""

from .catalog import (
    Action,
    Catalog,
    CatalogError,
    LimitError,
    MassRange,
    MAX_ENTITIES,
    MODE_EMPTY,
    MODE_INITIAL,
    MODE_RELABEL,
    Product,
    ProductMass,
    Snapshot,
    SRC_ATTACH,
    SRC_DETACH,
    SRC_INITIAL,
    SRC_MASS_CORRECTION,
    TRAILER,
    TRACTOR,
)
from .warehouse import (
    EntityContribution,
    EntityInterval,
    IN,
    LOAD,
    MAX_TICKETS,
    MAX_TRIPS,
    OUT,
    PairEvaluation,
    REASON_DIRECTION_IMPOSSIBLE,
    REASON_MASS_INTERVAL_CONFLICT,
    REASON_NO_PRODUCT,
    REASON_OK,
    REASON_ROLE_MISMATCH,
    REASON_TICKET_INFEASIBLE,
    REASON_TIME_ORDER,
    REASON_TRACTOR_MISMATCH,
    STATUS_CONFIRMED,
    STATUS_DRAFT,
    STATUS_REVIEW,
    STATUS_SUPERSEDED,
    Ticket,
    TicketBusy,
    TicketDerivation,
    TicketRejected,
    UNLOAD,
    Warehouse,
    WarehouseError,
)
from .partial_order import OrderEvent, PartialOrder, PartialOrderError
from .event_archive import (
    ArchiveError,
    EDGE_HANDOFF,
    EDGE_INTERVAL,
    EDGE_STATION,
    EventArchive,
    EventOrderReport,
    EvidenceCycle,
    KIND_OP,
    KIND_TRAILER,
    KIND_WEIGH,
    TrailerMismatch,
)
from .permit_replay import (
    MAX_CAPACITY,
    MAX_EVENTS,
    MAX_TOKEN_TYPES,
    PermitError,
    PermitReplay,
    PermitReplayEngine,
    PermitRule,
    PermitScheme,
    TokenType,
    Witness,
    WitnessStep,
    replay_permits,
)

__all__ = [
    "Action", "Catalog", "CatalogError", "LimitError", "MassRange", "MAX_ENTITIES",
    "MODE_EMPTY", "MODE_INITIAL", "MODE_RELABEL", "Product", "ProductMass",
    "Snapshot", "SRC_ATTACH", "SRC_DETACH", "SRC_INITIAL", "SRC_MASS_CORRECTION",
    "TRAILER", "TRACTOR",
    "EntityContribution", "EntityInterval", "IN", "OUT", "MAX_TICKETS", "MAX_TRIPS",
    "LOAD", "UNLOAD", "PairEvaluation", "Ticket", "TicketDerivation", "TicketBusy",
    "TicketRejected", "Warehouse", "WarehouseError",
    "REASON_OK", "REASON_TICKET_INFEASIBLE", "REASON_DIRECTION_IMPOSSIBLE",
    "REASON_MASS_INTERVAL_CONFLICT", "REASON_TIME_ORDER", "REASON_ROLE_MISMATCH",
    "REASON_TRACTOR_MISMATCH", "REASON_NO_PRODUCT",
    "STATUS_DRAFT", "STATUS_CONFIRMED", "STATUS_REVIEW", "STATUS_SUPERSEDED",
    # 事件偏序 / 事件档案
    "OrderEvent", "PartialOrder", "PartialOrderError",
    "EventArchive", "EventOrderReport", "ArchiveError", "EvidenceCycle",
    "TrailerMismatch", "EDGE_STATION", "EDGE_HANDOFF", "EDGE_INTERVAL",
    "KIND_WEIGH", "KIND_TRAILER", "KIND_OP",
    # 许可回放
    "PermitScheme", "PermitReplayEngine", "PermitReplay", "PermitRule",
    "TokenType", "Witness", "WitnessStep", "PermitError", "replay_permits",
    "MAX_EVENTS", "MAX_TOKEN_TYPES", "MAX_CAPACITY",
]
