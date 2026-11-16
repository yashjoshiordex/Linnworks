"""Unit tests for 3PL Pipeline Rule Engine (Milestones 1 & 2)."""

import pytest
from lwsync.pipeline.config import DEFAULT_SKU_SWAP_RULES, SkuPairRule
from lwsync.pipeline.rules import PipelineAction, RuleEngine


@pytest.fixture
def mock_stock_provider():
    """Mock stock level lookup function."""
    inventory = {
        ("US", "PRIMARY-SKU-001", "4d5833b6-a649-4d81-b572-da15682797ef"): {"on_hand": 50, "allocated": 5, "available": 45},
        ("US", "SUB-SKU-001", "4d5833b6-a649-4d81-b572-da15682797ef"): {"on_hand": 100, "allocated": 10, "available": 90},
        
        ("US", "PRIMARY-SKU-002", "4d5833b6-a649-4d81-b572-da15682797ef"): {"on_hand": 15, "allocated": 0, "available": 15},
        ("US", "SUB-SKU-002", "4d5833b6-a649-4d81-b572-da15682797ef"): {"on_hand": 60, "allocated": 10, "available": 50},

        ("CA", "PRIMARY-SKU-003", "faaab11f-76e5-469f-b105-d9472408f85c"): {"on_hand": 10, "allocated": 0, "available": 10},
        ("CA", "SUB-SKU-003", "faaab11f-76e5-469f-b105-d9472408f85c"): {"on_hand": 0, "allocated": 0, "available": 0},
    }

    def _lookup(region: str, sku: str, location_id: str):
        return inventory.get((region, sku, location_id), {"on_hand": 0, "allocated": 0, "available": 0})

    return _lookup


def test_scenario_a_stock_above_threshold_sends_primary(mock_stock_provider):
    """TC-01: Available (45) >= Threshold (20) -> SEND with original SKU."""
    engine = RuleEngine()
    decision = engine.evaluate_line_item("US", "PRIMARY-SKU-001", quantity=2, stock_lookup=mock_stock_provider)

    assert decision.action == PipelineAction.SEND
    assert decision.final_sku == "PRIMARY-SKU-001"
    assert decision.primary_available == 45
    assert decision.order_note is None
    assert len(decision.adjustments) == 0


def test_scenario_b_stock_below_threshold_swaps_to_substitute(mock_stock_provider):
    """TC-02: Available (15) < Threshold (30), Sub (50) >= Qty (2) -> SWAP."""
    engine = RuleEngine()
    decision = engine.evaluate_line_item("US", "PRIMARY-SKU-002", quantity=2, stock_lookup=mock_stock_provider)

    assert decision.action == PipelineAction.SWAP
    assert decision.final_sku == "SUB-SKU-002"
    assert decision.substitute_available == 50
    assert "AUTO-SWAP: Substituted PRIMARY-SKU-002 with SUB-SKU-002" in decision.order_note
    assert len(decision.adjustments) == 2
    # +2 to ordered SKU, -2 to substitute SKU
    assert decision.adjustments[0]["SKU"] == "PRIMARY-SKU-002"
    assert decision.adjustments[0]["Adjustment"] == 2
    assert decision.adjustments[1]["SKU"] == "SUB-SKU-002"
    assert decision.adjustments[1]["Adjustment"] == -2


def test_scenario_c_stock_and_substitute_insufficient_holds(mock_stock_provider):
    """TC-03: Available (10) < Threshold (50), Sub (0) < Qty (1) -> HOLD."""
    engine = RuleEngine()
    decision = engine.evaluate_line_item("CA", "PRIMARY-SKU-003", quantity=1, stock_lookup=mock_stock_provider)

    assert decision.action == PipelineAction.HOLD
    assert decision.final_sku == "PRIMARY-SKU-003"
    assert "Insufficient stock" in decision.reason


def test_unmonitored_sku_proceeds_normal(mock_stock_provider):
    """SKUs not in the 4 pairs matrix proceed with action SEND."""
    engine = RuleEngine()
    decision = engine.evaluate_line_item("US", "RANDOM-BATTERY-999", quantity=5, stock_lookup=mock_stock_provider)

    assert decision.action == PipelineAction.SEND
    assert decision.final_sku == "RANDOM-BATTERY-999"


def test_order_level_evaluation_hold_precedence(mock_stock_provider):
    """If one item in a multi-line order needs a HOLD, the entire order is placed on HOLD."""
    engine = RuleEngine()
    lines = [
        {"sku": "PRIMARY-SKU-001", "quantity": 1},  # Stock OK (45 >= 20) -> SEND
        {"sku": "PRIMARY-SKU-003", "quantity": 1},  # Out of stock in CA (10 < 50, sub=0) -> HOLD
    ]
    result = engine.evaluate_order("CA", "ORD-1001", lines, stock_lookup=mock_stock_provider)

    assert result.overall_action == PipelineAction.HOLD
    assert len(result.hold_reasons) == 1


def test_order_level_evaluation_swap_aggregate(mock_stock_provider):
    """If no items hold but an item swaps, overall order action is SWAP."""
    engine = RuleEngine()
    lines = [
        {"sku": "PRIMARY-SKU-001", "quantity": 1},  # Stock OK -> SEND
        {"sku": "PRIMARY-SKU-002", "quantity": 3},  # Stock low (15 < 30) -> SWAP
    ]
    result = engine.evaluate_order("US", "ORD-1002", lines, stock_lookup=mock_stock_provider)

    assert result.overall_action == PipelineAction.SWAP
    assert len(result.notes_to_add) == 1
    assert len(result.adjustments_to_post) == 2

