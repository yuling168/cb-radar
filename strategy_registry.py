"""Central definitions for strategies exposed by the dashboard and runners."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_code: str
    active_version: str
    strategy_name: str


_STRATEGIES = {
    "A": StrategyDefinition("A", "v2", "CB 成交量創 10 日新高"),
    "B": StrategyDefinition("B", "v1", "CB 突破轉換價"),
    "C": StrategyDefinition("C", "v1", "CB 資優生"),
    "G": StrategyDefinition("G", "v1", "時間發動策略"),
}


def get_strategy(strategy_code: str) -> StrategyDefinition:
    """Return the centrally declared active definition for a strategy."""
    try:
        return _STRATEGIES[strategy_code]
    except KeyError as exc:
        raise ValueError(f"Unknown strategy code: {strategy_code!r}") from exc


def active_strategy_codes() -> tuple[str, ...]:
    """Return dashboard strategies in their established display order."""
    return tuple(_STRATEGIES)
