"""Rule Evaluation Engine for the 3PL Order Processing Pipeline."""

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

from lwsync.logger import logger
from lwsync.pipeline.config import PipelineConfig, SkuPairRule


class PipelineAction(str, Enum):
    """Fulfillment decisions for order routing."""
    SEND = "send"
    SWAP = "swap"
    HOLD = "hold"
    ERROR = "error"


@dataclass
class RuleDecision:
    """Individual line item evaluation outcome."""
    action: PipelineAction
    original_sku: str
    final_sku: str
    quantity: int
    primary_available: int = 0
    substitute_available: int = 0
    rule: Optional[SkuPairRule] = None
    reason: str = ""
    order_note: Optional[str] = None
    adjustments: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class OrderEvaluationResult:
    """Comprehensive evaluation outcome for an entire order."""
    region: str
    order_id: str
    overall_action: PipelineAction
    line_decisions: List[RuleDecision]
    notes_to_add: List[str] = field(default_factory=list)
    adjustments_to_post: List[Dict[str, Any]] = field(default_factory=list)
    hold_reasons: List[str] = field(default_factory=list)


# Type alias for stock lookup function: (region: str, sku: str, location_id: str) -> dict(on_hand, allocated, available)
StockLookupCallable = Callable[[str, str, str], Dict[str, int]]


class RuleEngine:
    """Inventory rule engine evaluating 4-SKU swap pairs and stock threshold rules."""

    def __init__(self, rules: Optional[List[SkuPairRule]] = None):
        self.rules: List[SkuPairRule] = rules if rules is not None else PipelineConfig.SKU_SWAP_RULES
        # Map: (region_upper, primary_sku_upper) -> SkuPairRule
        self._rules_map: Dict[tuple[str, str], SkuPairRule] = {
            (r.region.upper(), r.primary_sku.upper()): r for r in self.rules
        }

    def get_rule_for_sku(self, region: str, sku: str) -> Optional[SkuPairRule]:
        """Lookup active rule matching region and primary SKU."""
        return self._rules_map.get((region.upper(), sku.upper()))

    def evaluate_line_item(
        self,
        region: str,
        sku: str,
        quantity: int,
        stock_lookup: StockLookupCallable,
    ) -> RuleDecision:
        """Evaluate a single order line item against inventory rules."""
        region_key = region.upper()
        rule = self.get_rule_for_sku(region_key, sku)

        # Case 1: SKU is not in the monitored swap pairs matrix
        if not rule:
            return RuleDecision(
                action=PipelineAction.SEND,
                original_sku=sku,
                final_sku=sku,
                quantity=quantity,
                reason="SKU is not subject to substitution rules.",
            )

        # Fetch inventory for primary SKU at the designated warehouse location
        primary_stock = stock_lookup(region_key, rule.primary_sku, rule.location_id)
        primary_on_hand = primary_stock.get("on_hand", 0)
        primary_allocated = primary_stock.get("allocated", 0)
        primary_available = primary_stock.get("available", primary_on_hand - primary_allocated)

        logger.debug(
            f"[{region_key}] Evaluating rule for SKU '{rule.primary_sku}': "
            f"Available={primary_available}, Threshold={rule.threshold}, Required={quantity}"
        )

        # Scenario A: Primary stock meets or exceeds threshold
        if primary_available >= rule.threshold:
            return RuleDecision(
                action=PipelineAction.SEND,
                original_sku=rule.primary_sku,
                final_sku=rule.primary_sku,
                quantity=quantity,
                primary_available=primary_available,
                rule=rule,
                reason=(
                    f"Stock is healthy: available ({primary_available}) >= "
                    f"threshold ({rule.threshold}). Proceeding with primary SKU."
                ),
            )

        # Scenario B & C: Primary stock is below threshold -> Check Substitute SKU
        sub_stock = stock_lookup(region_key, rule.substitute_sku, rule.location_id)
        sub_on_hand = sub_stock.get("on_hand", 0)
        sub_allocated = sub_stock.get("allocated", 0)
        sub_available = sub_stock.get("available", sub_on_hand - sub_allocated)

        # Scenario B: Substitute SKU has enough stock to cover required quantity
        if sub_available >= quantity:
            note = (
                f"AUTO-SWAP: Substituted {rule.primary_sku} with {rule.substitute_sku}. "
                f"Reason: Stock below threshold (Available: {primary_available}, Threshold: {rule.threshold})."
            )
            adjustments = [
                {
                    "SKU": rule.primary_sku,
                    "LocationId": rule.location_id,
                    "Adjustment": quantity,
                    "Reason": "Pipeline Swap Correction (+Ordered SKU)",
                },
                {
                    "SKU": rule.substitute_sku,
                    "LocationId": rule.location_id,
                    "Adjustment": -quantity,
                    "Reason": "Pipeline Swap Correction (-Shipped SKU)",
                },
            ]
            return RuleDecision(
                action=PipelineAction.SWAP,
                original_sku=rule.primary_sku,
                final_sku=rule.substitute_sku,
                quantity=quantity,
                primary_available=primary_available,
                substitute_available=sub_available,
                rule=rule,
                reason=(
                    f"Primary available ({primary_available}) < threshold ({rule.threshold}). "
                    f"Swapped to substitute {rule.substitute_sku} (Available: {sub_available})."
                ),
                order_note=note,
                adjustments=adjustments,
            )

        # Scenario C: Both Primary and Substitute have insufficient stock -> Hold/Park
        hold_reason = (
            f"Insufficient stock for primary SKU '{rule.primary_sku}' "
            f"(Available: {primary_available}, Threshold: {rule.threshold}) "
            f"and substitute SKU '{rule.substitute_sku}' "
            f"(Available: {sub_available}, Required: {quantity})."
        )
        return RuleDecision(
            action=PipelineAction.HOLD,
            original_sku=rule.primary_sku,
            final_sku=rule.primary_sku,
            quantity=quantity,
            primary_available=primary_available,
            substitute_available=sub_available,
            rule=rule,
            reason=hold_reason,
        )

    def evaluate_order(
        self,
        region: str,
        order_id: str,
        line_items: List[Dict[str, Any]],
        stock_lookup: StockLookupCallable,
    ) -> OrderEvaluationResult:
        """Evaluate all lines in an order and determine the overall order disposition."""
        decisions: List[RuleDecision] = []
        notes_to_add: List[str] = []
        adjustments: List[Dict[str, Any]] = []
        hold_reasons: List[str] = []

        has_hold = False
        has_swap = False

        for line in line_items:
            sku = line.get("sku") or line.get("SKU") or line.get("ItemNumber") or ""
            qty = int(line.get("quantity") or line.get("Quantity") or line.get("Qty") or 1)

            decision = self.evaluate_line_item(region, sku, qty, stock_lookup)
            decisions.append(decision)

            if decision.action == PipelineAction.HOLD:
                has_hold = True
                hold_reasons.append(decision.reason)
            elif decision.action == PipelineAction.SWAP:
                has_swap = True
                if decision.order_note:
                    notes_to_add.append(decision.order_note)
                if decision.adjustments:
                    adjustments.extend(decision.adjustments)

        # Order-level disposition logic:
        # If ANY item must be held, the entire order is placed on hold (do not dispatch partial orders to 3PL)
        if has_hold:
            overall_action = PipelineAction.HOLD
        elif has_swap:
            overall_action = PipelineAction.SWAP
        else:
            overall_action = PipelineAction.SEND

        return OrderEvaluationResult(
            region=region.upper(),
            order_id=str(order_id),
            overall_action=overall_action,
            line_decisions=decisions,
            notes_to_add=notes_to_add,
            adjustments_to_post=adjustments,
            hold_reasons=hold_reasons,
        )

