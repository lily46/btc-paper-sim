"""設定ファイルの読み込み。金額系はすべて Decimal に変換する。"""
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = ROOT / "config.json"

# Decimal に変換するキー（値が文字列で書かれている前提）
_DECIMAL_KEYS = {
    "initial_capital_jpy", "taker_fee_rate", "slippage_bps",
    "max_order_pct_of_initial", "max_btc_pct_of_equity", "max_drawdown_stop",
    "min_order_size_btc", "min_jpy_reserve", "max_price_jump_pct",
    "min_price_jpy", "max_price_jpy", "max_spread_pct",
    "order_pct", "buy_below", "sell_above", "allocation_pct",
    # 戦略 E（短期モメンタム）
    "entry_momentum_pct", "entry_long_momentum_pct", "entry_max_spread_pct",
    "order_jpy", "take_profit_pct", "stop_loss_pct", "trend_reversal_pct",
}


def _convert(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: (Decimal(str(v)) if k in _DECIMAL_KEYS else _convert(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_convert(v) for v in obj]
    return obj


class Config(dict):
    """dict ベースの薄いラッパ。config["risk"]["max_drawdown_stop"] の形で参照する。"""

    @classmethod
    def load(cls, path: str | Path | None = None) -> "Config":
        p = Path(path) if path else DEFAULT_CONFIG_PATH
        raw = json.loads(p.read_text(encoding="utf-8"))
        return cls(_convert(raw))

    @property
    def strategy_ids(self) -> list[str]:
        return [k for k, v in self["strategies"].items() if v.get("enabled", True)]
