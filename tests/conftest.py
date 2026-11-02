from __future__ import annotations

import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from btcsim.config import Config  # noqa: E402
from btcsim.portfolio import Portfolio  # noqa: E402


@pytest.fixture()
def cfg() -> Config:
    return Config.load(ROOT / "config.json")


@pytest.fixture()
def pf(cfg) -> Portfolio:
    cap = cfg["experiment"]["initial_capital_jpy"]
    return Portfolio(strategy_id="T", initial_jpy=cap, jpy=cap)


@pytest.fixture()
def board():
    """十分に厚い板（1 レベルあたり 2 BTC）。"""
    asks = [(Decimal(10_000_000 + 1000 * i), Decimal("2")) for i in range(30)]
    bids = [(Decimal(9_999_000 - 1000 * i), Decimal("2")) for i in range(30)]
    return {"asks": asks, "bids": bids}
