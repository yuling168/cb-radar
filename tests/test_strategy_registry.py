import pytest

from strategy_registry import active_strategy_codes, get_strategy


def test_registry_declares_active_dashboard_strategies():
    assert active_strategy_codes() == ("A", "B", "C", "G")
    assert (get_strategy("A").active_version, get_strategy("A").strategy_name) == (
        "v2", "CB 成交量創 10 日新高",
    )


def test_registry_rejects_unknown_strategy_code():
    with pytest.raises(ValueError, match="Unknown strategy code"):
        get_strategy("unknown")
