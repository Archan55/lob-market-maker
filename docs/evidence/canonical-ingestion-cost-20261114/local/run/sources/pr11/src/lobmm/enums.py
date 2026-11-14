"""Stable enumerations shared across the simulator."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class Side(IntEnum):
    """Resting-book side; also the signed inventory direction for an order."""

    BID = 1
    ASK = -1

    @property
    def opposite(self) -> Side:
        return Side(-int(self))


class EventType(StrEnum):
    ADD = "ADD"
    CANCEL = "CANCEL"
    TRADE = "TRADE"
    RESET = "RESET"
    SNAPSHOT = "SNAPSHOT"


class ValidationMode(StrEnum):
    STRICT = "strict"
    LENIENT = "lenient"


class QueueAllocation(StrEnum):
    BACK_OF_QUEUE = "back_of_queue"
    PRO_RATA = "pro_rata"
    FRONT_OF_QUEUE = "front_of_queue"


class OrderStatus(StrEnum):
    PENDING_ARRIVAL = "pending_arrival"
    LIVE = "live"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    EXPIRED = "expired"
    REJECTED = "rejected"

    @property
    def terminal(self) -> bool:
        return self in {
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.REJECTED,
        }


class LiquidityRole(StrEnum):
    MAKER = "maker"
    TAKER = "taker"


class ReportType(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    PARTIAL_FILL = "partial_fill"
    FILL = "fill"
    CANCELLED = "cancelled"
    CANCEL_REJECTED = "cancel_rejected"
    EXPIRED = "expired"


class SchedulerPhase(IntEnum):
    MARKET = 10
    SESSION = 15
    EXCHANGE_COMMAND = 20
    MARKET_DATA_DELIVERY = 30
    EXECUTION_REPORT_DELIVERY = 31
    TIMER = 32
    STRATEGY_DECISION = 40
    AUDIT = 90


class MarkPrice(StrEnum):
    MIDPOINT = "midpoint"
    MICROPRICE = "microprice"
    CONSERVATIVE = "conservative"


class SessionEndPolicy(StrEnum):
    MARK = "mark"
    LIQUIDATE = "liquidate"


class StrategyName(StrEnum):
    FIXED_SPREAD = "fixed_spread"
    INVENTORY_AWARE = "inventory_aware"
    MICROPRICE = "microprice"


class RiskReason(StrEnum):
    KILL_SWITCH = "kill_switch"
    MAX_ORDER_SIZE = "max_order_size"
    POSITION_LIMIT = "position_limit"
    MAX_OPEN_QUANTITY = "max_open_quantity"
    MAX_OPEN_ORDERS = "max_open_orders"
    MAX_LOSS = "max_loss"
    MAX_DRAWDOWN = "max_drawdown"
    QUOTE_AGE = "quote_age"
    SPREAD_TOO_NARROW = "spread_too_narrow"
    VOLATILITY_TOO_HIGH = "volatility_too_high"
    MESSAGE_RATE = "message_rate"
    SESSION_END = "session_end"
    INVALID_ORDER = "invalid_order"
