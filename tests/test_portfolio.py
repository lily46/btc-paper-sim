from decimal import Decimal

import pytest

from btcsim.portfolio import Portfolio, walk_book

D = Decimal


def buy(pf, cfg, board, notional, key="k1"):
    return pf.buy(ts="t", notional_jpy=notional, asks=board["asks"],
                  mark_price=board["bids"][0][0], cfg=cfg, signal_key=key, reason="test")


def sell(pf, cfg, board, size, key="k2"):
    return pf.sell(ts="t", size_btc=size, bids=board["bids"],
                   mark_price=board["bids"][0][0], cfg=cfg, signal_key=key, reason="test")


def test_walk_book_vwap_and_depth():
    levels = [(D(100), D(1)), (D(110), D(1))]
    assert walk_book(levels, D("1.5")) == (D("1.5"), D(100) * D(1) / D("1.5") + D(110) * D("0.5") / D("1.5"))
    filled, _ = walk_book(levels, D(5))     # 板が薄ければ数量が減る
    assert filled == D(2)


def test_buy_applies_fee_and_slippage(pf, cfg, board):
    tr = buy(pf, cfg, board, D(200_000))
    assert tr is not None and tr.side == "BUY"
    # 手数料 0.15% が JPY から引かれている
    assert Decimal(tr.fee_jpy) == (Decimal(tr.gross_jpy) * cfg["fees"]["taker_fee_rate"]).quantize(Decimal("0.0001"))
    assert pf.jpy == D(1_000_000) - Decimal(tr.gross_jpy) - Decimal(tr.fee_jpy)
    assert pf.btc > 0
    # スリッページにより best_ask より不利な価格で約定
    assert Decimal(tr.price) > board["asks"][0][0]


def test_order_capped_at_20pct_of_initial(pf, cfg, board):
    tr = buy(pf, cfg, board, D(900_000))
    assert tr is not None
    assert Decimal(tr.gross_jpy) <= D(1_000_000) * cfg["risk"]["max_order_pct_of_initial"]


def test_btc_ratio_capped_at_80pct(pf, cfg, board):
    for i in range(10):
        buy(pf, cfg, board, D(200_000), key=f"k{i}")
    mark = board["bids"][0][0]
    ratio = pf.btc * mark / pf.equity(mark)
    assert ratio <= cfg["risk"]["max_btc_pct_of_equity"] + Decimal("0.001")
    assert pf.jpy > 0


def test_cannot_exceed_balance(pf, cfg, board):
    pf.jpy = D(100)                       # 最低数量すら買えない
    assert buy(pf, cfg, board, D(200_000)) is None
    assert pf.jpy == D(100)


def test_no_short_selling(pf, cfg, board):
    assert sell(pf, cfg, board, D(1)) is None      # 保有ゼロでは売れない
    buy(pf, cfg, board, D(200_000))
    held = pf.btc
    sell(pf, cfg, board, D(99))                     # 保有量を超える売却は保有量に制限
    assert pf.btc == D(0)
    assert held > 0


def test_min_order_size_enforced(pf, cfg, board):
    # 0.001 BTC 未満になる極小注文は約定しない
    assert buy(pf, cfg, board, D(5_000)) is None


def test_size_precision(pf, cfg, board):
    tr = buy(pf, cfg, board, D(200_000))
    assert -Decimal(tr.size).as_tuple().exponent <= cfg["risk"]["size_precision"]


def test_realized_pnl_and_fees(pf, cfg, board):
    buy(pf, cfg, board, D(200_000))
    before_fees = pf.fees_paid
    tr = sell(pf, cfg, board, pf.btc)
    assert tr is not None
    assert pf.fees_paid > before_fees
    # スプレッド+手数料ぶん必ず負け（同じ板で即往復した場合）
    assert Decimal(tr.realized_pnl) < 0
    assert pf.realized_pnl == Decimal(tr.realized_pnl)
    assert pf.btc == D(0) and pf.cost_basis_jpy == D(0)
    assert pf.wins + pf.losses == 1


def test_drawdown_halt(pf, cfg):
    pf.mark(D(0), cfg["risk"]["max_drawdown_stop"])          # peak = 1,000,000
    pf.jpy = D(0)
    pf.btc = D(1)
    pf.mark(D(880_000), cfg["risk"]["max_drawdown_stop"])    # -12%
    assert pf.halted and pf.max_drawdown >= Decimal("0.10")


def test_no_halt_within_threshold(pf, cfg):
    pf.mark(D(0), cfg["risk"]["max_drawdown_stop"])
    pf.jpy = D(0)
    pf.btc = D(1)
    pf.mark(D(950_000), cfg["risk"]["max_drawdown_stop"])
    assert not pf.halted


def test_roundtrip_serialization(pf, cfg, board):
    buy(pf, cfg, board, D(200_000))
    clone = Portfolio.from_dict(pf.to_dict())
    assert clone.to_dict() == pf.to_dict()
    assert isinstance(clone.jpy, Decimal)


def test_thin_book_limits_fill(pf, cfg):
    thin = {"asks": [(D(10_000_000), Decimal("0.004"))],
            "bids": [(D(9_990_000), Decimal("0.004"))]}
    tr = buy(pf, cfg, thin, D(200_000))
    assert tr is not None
    assert Decimal(tr.size) <= Decimal("0.004")
