from decimal import Decimal

from btcsim import strategies
from btcsim.portfolio import Portfolio

D = Decimal
STEP = {"30m": 1800, "1h": 3600}


def mk_bars(closes, tf="30m", start=0):
    step = STEP[tf]
    return [{"ts": start + i * step, "o": str(c), "h": str(c), "l": str(c), "c": str(c), "v": "1"}
            for i, c in enumerate(closes)]


def now_after(bars, tf="30m"):
    return bars[-1]["ts"] + STEP[tf]      # 最後の足が確定した直後


def fresh_pf():
    return Portfolio(strategy_id="T", initial_jpy=D(1_000_000), jpy=D(1_000_000))


def test_a_holds_during_warmup(cfg):
    bars = {"30m": mk_bars([D(100)] * 5)}
    sig = strategies.strategy_a(cfg, bars, now_after(bars["30m"]), fresh_pf())
    assert sig.action == "HOLD" and "warmup" in sig.reason


def test_a_golden_cross_buys(cfg):
    closes = [D(100)] * 30 + [D(100 + 5 * i) for i in range(1, 10)]
    bars = {"30m": mk_bars(closes)}
    sig = strategies.strategy_a(cfg, bars, now_after(bars["30m"]), fresh_pf())
    # 上昇転換の過程でどこかで必ずゴールデンクロスが起きる
    actions = []
    for n in range(31, len(closes) + 1):
        b = {"30m": mk_bars(closes[:n])}
        actions.append(strategies.strategy_a(cfg, b, now_after(b["30m"]), fresh_pf()).action)
    assert "BUY" in actions
    assert sig.action in {"BUY", "HOLD"}


def test_a_dead_cross_sells(cfg):
    closes = [D(100 + 5 * i) for i in range(30)] + [D(245 - 20 * i) for i in range(1, 12)]
    actions = []
    for n in range(31, len(closes) + 1):
        b = {"30m": mk_bars(closes[:n])}
        actions.append(strategies.strategy_a(cfg, b, now_after(b["30m"]), fresh_pf()).action)
    assert "SELL" in actions


def test_a_signal_key_is_stable_per_bar(cfg):
    closes = [D(100)] * 30 + [D(100 + 5 * i) for i in range(1, 10)]
    bars = {"30m": mk_bars(closes)}
    n = now_after(bars["30m"])
    k1 = strategies.strategy_a(cfg, bars, n, fresh_pf()).key
    k2 = strategies.strategy_a(cfg, bars, n, fresh_pf()).key
    assert k1 == k2        # 同じ足では同じキー -> 二重発注されない


def test_b_buys_when_entering_oversold(cfg):
    closes = [D(100)] * 20 + [D(100 - 3 * i) for i in range(1, 8)]
    bars = {"30m": mk_bars(closes)}
    sig = strategies.strategy_b(cfg, bars, now_after(bars["30m"]), fresh_pf())
    assert sig.action in {"BUY", "HOLD"}
    found = any(
        strategies.strategy_b(cfg, {"30m": mk_bars(closes[:n])},
                              now_after(mk_bars(closes[:n])), fresh_pf()).action == "BUY"
        for n in range(17, len(closes) + 1)
    )
    assert found


def test_b_sells_when_entering_overbought(cfg):
    closes = [D(100)] * 20 + [D(100 + 3 * i) for i in range(1, 8)]
    found = any(
        strategies.strategy_b(cfg, {"30m": mk_bars(closes[:n])},
                              now_after(mk_bars(closes[:n])), fresh_pf()).action == "SELL"
        for n in range(17, len(closes) + 1)
    )
    assert found


def test_b_no_repeat_in_deep_oversold(cfg):
    """RSI が売られすぎに入り続けている間は（クロス時のみなので）連発しない。"""
    closes = [D(1000 - 5 * i) for i in range(40)]
    sig = strategies.strategy_b(cfg, {"30m": mk_bars(closes)}, now_after(mk_bars(closes)), fresh_pf())
    assert sig.action == "HOLD"


def test_c_requires_uptrend_for_entry(cfg):
    # 1h は下降トレンド、30m の RSI は売られすぎ -> 買わない
    down_1h = mk_bars([D(1000 - 10 * i) for i in range(40)], tf="1h")
    dip_30m = mk_bars([D(600)] * 20 + [D(600 - 10 * i) for i in range(1, 8)])
    pf = fresh_pf()
    sig = strategies.strategy_c(cfg, {"1h": down_1h, "30m": dip_30m},
                                max(now_after(down_1h, "1h"), now_after(dip_30m)), pf)
    assert sig.action == "HOLD"     # 保有ゼロなので売りも出ない


def test_c_sells_when_trend_turns_down(cfg):
    down_1h = mk_bars([D(1000 - 10 * i) for i in range(40)], tf="1h")
    flat_30m = mk_bars([D(600)] * 25)
    pf = fresh_pf()
    pf.btc = D(1)
    pf.cost_basis_jpy = D(500_000)
    sig = strategies.strategy_c(cfg, {"1h": down_1h, "30m": flat_30m},
                                max(now_after(down_1h, "1h"), now_after(flat_30m)), pf)
    assert sig.action == "SELL"


def test_c_warmup_blocks_trading(cfg):
    bars = {"1h": mk_bars([D(100)] * 5, tf="1h"), "30m": mk_bars([D(100)] * 5)}
    sig = strategies.strategy_c(cfg, bars, now_after(bars["30m"]), fresh_pf())
    assert sig.action == "HOLD" and "warmup" in sig.reason


def test_d_buys_once_then_holds(cfg):
    pf = fresh_pf()
    sig = strategies.strategy_d(cfg, {}, 0, pf)
    assert sig.action == "BUY" and sig.order_pct == D(1)
    pf.btc = D("0.05")
    pf.bootstrapped = True
    assert strategies.strategy_d(cfg, {}, 0, pf).action == "HOLD"


def test_no_lookahead_unclosed_bar_ignored(cfg):
    """進行中の足を無視していることを確認（足を1本足しても now を進めないと結果が変わらない）。"""
    closes = [D(100)] * 30
    bars = {"30m": mk_bars(closes)}
    now = now_after(bars["30m"])
    base = strategies.strategy_a(cfg, bars, now, fresh_pf())
    spiked = {"30m": mk_bars(closes + [D(100000)])}
    after = strategies.strategy_a(cfg, spiked, now, fresh_pf())
    assert base.reason == after.reason
