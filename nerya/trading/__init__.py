"""Trading kernel — intents, plans, risk gate, approval gate, executors,
capital, position book, paper execution, ledger.

04-29 introduces a richer control plane on top of the
existing safety scaffolding (kill switch + live flag + RiskGate +
ApprovalGate + ExecutionEngine). New code should import from the
explicit submodules; the names re-exported here are the stable ones
every dashboard / CLI / SDK call site already depends on.
"""

from .accounts import (
    Account,
    AccountLimits,
    AccountPermissions,
    AccountProfile,
    get_account,
    get_account_profile,
    load_account_profiles,
    load_accounts,
)
from .account_snapshots import (
    AccountSnapshot,
    capture_all,
    capture_snapshot,
    fresh_snapshot,
    latest_snapshot,
    latest_snapshots,
)
from .account_refresh import account_refresh_interval_seconds, refresh_account_marks
from .approval import ApprovalGate, ApprovalRecord
from .capital import (
    BudgetChecker,
    BudgetDecision,
    CapitalReservation,
    CapitalReservationStore,
)
from .execution import ExecutionEngine
from .executors import (
    Executor,
    ExecutorOrchestrator,
    MarketOrderExecutor,
    PositionProtectionExecutor,
)
from .intents import TradeIntent
from .order_intents import (
    OrderCandidate,
    ProtectionRule,
    SizingPolicy,
    StopLossSpec,
    TakeProfitSpec,
    TrailingStopSpec,
    TradePlan,
)
from .order_tracker import (
    OrderTracker,
    TrackedFill,
    TrackedOrder,
    make_client_order_id,
)
from .orders import Fill, OrderRequest, OrderResult
from .position_book import Position, PositionBook
from .protection_store import ProtectionStore, evaluate as evaluate_protection
from .reconciliation import (
    ReconciliationReport,
    ReconciliationStore,
    reconcile,
    reconcile_account,
    reconcile_local,
    reconcile_strategy,
)
from .risk import RiskDecision, RiskGate
from .promotion import (
    BACKTEST_EQUIVALENT_EVIDENCE,
    EvidenceStore,
    PromotionDecision,
    PromotionRecord,
    PromotionStore,
    REQUIRED_EVIDENCE,
    StrategyEvidence,
    apply_promotion,
    evaluate_promotion,
    request_promotion,
)
from .strategy_lifecycle import (
    ACCOUNT_BINDABLE_STATES,
    ALLOWED_TRANSITIONS,
    EXECUTING_STATES,
    PROMOTION_TARGETS,
    STATES,
    InvalidTransition,
    LIVE_STATES,
    TRADABLE_STATES,
    is_account_bindable,
    is_executing,
    is_live,
    is_tradable,
    promotion_target,
    validate_transition,
)
from .submit import submit_trade_intent, submit_trade_plan
from .virtual_ledger import VirtualLedger

__all__ = [
    # Legacy / stable surface
    "TradeIntent",
    "OrderRequest", "OrderResult", "Fill",
    "RiskDecision", "RiskGate",
    "ApprovalGate", "ApprovalRecord",
    "ExecutionEngine",
    "VirtualLedger",
    "STATES", "TRADABLE_STATES", "LIVE_STATES",
    "EXECUTING_STATES", "ACCOUNT_BINDABLE_STATES",
    "ALLOWED_TRANSITIONS", "PROMOTION_TARGETS",
    "InvalidTransition",
    "validate_transition", "is_tradable", "is_live",
    "is_executing", "is_account_bindable", "promotion_target",
    # Promotion gate
    "PromotionDecision", "PromotionRecord", "PromotionStore",
    "EvidenceStore", "StrategyEvidence", "REQUIRED_EVIDENCE",
    "BACKTEST_EQUIVALENT_EVIDENCE",
    "evaluate_promotion", "request_promotion", "apply_promotion",
    # Accounts
    "Account", "AccountLimits", "AccountPermissions", "AccountProfile",
    "load_accounts", "load_account_profiles", "get_account", "get_account_profile",
    # Snapshots
    "AccountSnapshot", "capture_snapshot", "capture_all",
    "latest_snapshot", "latest_snapshots", "fresh_snapshot",
    "account_refresh_interval_seconds", "refresh_account_marks",
    # Capital / budget
    "BudgetChecker", "BudgetDecision",
    "CapitalReservation", "CapitalReservationStore",
    # Schemas
    "OrderCandidate", "ProtectionRule", "SizingPolicy",
    "StopLossSpec", "TakeProfitSpec", "TrailingStopSpec", "TradePlan",
    # Order tracker / position book
    "OrderTracker", "TrackedFill", "TrackedOrder", "make_client_order_id",
    "Position", "PositionBook",
    "ProtectionStore", "evaluate_protection",
    # Executors
    "Executor", "ExecutorOrchestrator",
    "MarketOrderExecutor", "PositionProtectionExecutor",
    # Reconciliation
    "ReconciliationReport", "ReconciliationStore",
    "reconcile", "reconcile_local", "reconcile_account",
    "reconcile_strategy",
    # Submit entry points
    "submit_trade_intent", "submit_trade_plan",
]
