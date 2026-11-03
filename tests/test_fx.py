"""FX（証拠金取引・空売りあり）モデルのテスト。"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from btcsim.config import Config, FX_CONFIG_PATH
from btcsim.fx import run as fxrun
from btcsim.fx.account import LONG, SHORT, MarginAccount
from btcsim.fx.strategies import strategy_f, strategy_g, strategy_h
from btcsim.store import Store

D = Decimal
BASE = 1_700_000_000
PRICE = D(12_000_000)


@pytest.fixture()
def fxcfg():
    return Config.load(FX_CONFIG_PATH)


@pytest.fixture()
def acc():
    return MarginAccount(strategy_id="F_momentum_ls", initial_jpy=D(1_000_000),
                         collateral_jpy=D(1_000_000))


def mk_board(price=PRICE, depth=30, size="2"):
    return {"asks": [(price + D(2000) + D(1000) * i, D(size)) for i in range(depth)],
            "bids": [(price - D(2000) - D(1000) * i, D(size)) for i in range(depth)]}


def quotes(price=PRICE):
    return price - D(2000), price + D(2000)      # bid, ask


def mk_ticks(prices, now_ts=BASE, step_min=10):
    n = len(prices)
    return [{"tick_id": str(now_ts - (n - 1 - i) * step_min * 60),
             "ts": now_ts - (n - 1 - i) * step_min * 60,
             "ltp": str(p), "best_bid": str(p), "best_ask": str(p)}
            for i, p in enumerate(prices)]


def open_pos(acc, fxcfg, side, price=PRICE, notional=D(200_000), now_ts=BASE):
    bid, ask = quotes(price)
    return acc.open_position(ts="t", side=side, notional_jpy=notional, board=mk_board(price),
                             bid=bid, ask=ask, cfg=fxcfg, signal_key="k", reason="test",
                             now_ts=now_ts, spread_pct=D("0.0003"))


# --- ショートの損益計算 --------------------------------------------------

def test_short_profits_when_price_falls(acc, fxcfg):
    tr = open_pos(acc, fxcfg, SHORT)
    assert tr is not None and tr.side == SHORT and tr.action == "OPEN"
    entry = Decimal(tr.price)

    # 5% 下落 → ショートは含み益
    lower = PRICE * D("0.95")
    bid, ask = quotes(lower)
    assert acc.unrealized(bid, ask) > 0
    assert acc.pnl_pct(bid, ask) > D("0.04")

    closed = acc.close_position(ts="t2", board=mk_board(lower), bid=bid, ask=ask, cfg=fxcfg,
                                signal_key="k2", reason="tp", now_ts=BASE + 3600,
                                exit_kind="take_profit")
    assert closed is not None and closed.action == "CLOSE"
    assert Decimal(closed.realized_pnl) > 0
    assert acc.collateral_jpy > D(1_000_000)
    assert acc.position is None and acc.wins == 1
    assert Decimal(closed.hold_minutes) == pytest.approx(60, abs=1)
    assert Decimal(closed.entry_price) == entry


def test_short_loses_when_price_rises(acc, fxcfg):
    open_pos(acc, fxcfg, SHORT)
    higher = PRICE * D("1.03")
    bid, ask = quotes(higher)
    assert acc.unrealized(bid, ask) < 0
    tr = acc.close_position(ts="t", board=mk_board(higher), bid=bid, ask=ask, cfg=fxcfg,
                            signal_key="k", reason="sl", now_ts=BASE + 600,
                            exit_kind="stop_loss")
    assert Decimal(tr.realized_pnl) < 0
    assert acc.collateral_jpy < D(1_000_000)
    assert acc.losses == 1


def test_long_mirrors_short(acc, fxcfg):
    open_pos(acc, fxcfg, LONG)
    higher = PRICE * D("1.05")
    bid, ask = quotes(higher)
    assert acc.unrealized(bid, ask) > 0
    lower = PRICE * D("0.95")
    bid2, ask2 = quotes(lower)
    assert acc.unrealized(bid2, ask2) < 0


def test_valuation_uses_exit_side(acc, fxcfg):
    """ロングは bid 側、ショートは ask 側で評価する（保守的）。"""
    open_pos(acc, fxcfg, LONG)
    bid, ask = quotes()
    assert acc.exit_price_ref(bid, ask) == bid
    acc.position = None
    open_pos(acc, fxcfg, SHORT)
    assert acc.exit_price_ref(bid, ask) == ask


# --- レバレッジと証拠金 --------------------------------------------------

def test_notional_capped_by_leverage(acc, fxcfg):
    tr = open_pos(acc, fxcfg, LONG, notional=D(10_000_000))     # 1000万を要求
    assert tr is not None
    # 証拠金 100 万 × レバレッジ 2 倍 = 200 万が上限
    assert Decimal(tr.notional_jpy) <= D(2_000_000) + D(1)


def test_margin_ratio_and_effective_leverage(acc, fxcfg):
    open_pos(acc, fxcfg, LONG, notional=D(2_000_000))
    bid, ask = quotes()
    ratio = acc.margin_ratio(bid, ask, fxcfg["risk"]["leverage"])
    # 想定元本200万 / レバ2倍 = 必要証拠金100万、有効証拠金≒100万 → 維持率≒100%
    assert D("0.9") < ratio < D("1.1")


def test_min_order_size_enforced(acc, fxcfg):
    # FX の最低数量は 0.01 BTC = 12 万円相当。5 万円では建たない
    assert open_pos(acc, fxcfg, LONG, notional=D(50_000)) is None
    assert acc.position is None


def test_cannot_open_two_positions(acc, fxcfg):
    assert open_pos(acc, fxcfg, LONG) is not None
    assert open_pos(acc, fxcfg, SHORT) is None          # 同時 1 建玉


def test_fees_follow_config(acc, fxcfg):
    """FX は既定で取引手数料 0%（bitFlyer の実際の料金体系）。"""
    tr = open_pos(acc, fxcfg, LONG)
    assert Decimal(tr.fee_jpy) == 0
    assert acc.collateral_jpy == D(1_000_000)

    # 手数料を設定すれば引かれる
    cfg2 = {**fxcfg, "fees": {**fxcfg["fees"], "taker_fee_rate": D("0.001")}}
    acc2 = MarginAccount(strategy_id="X", initial_jpy=D(1_000_000), collateral_jpy=D(1_000_000))
    tr2 = acc2.open_position(ts="t", side=LONG, notional_jpy=D(200_000), board=mk_board(),
                             bid=quotes()[0], ask=quotes()[1], cfg=cfg2, signal_key="k",
                             reason="r", now_ts=BASE)
    assert Decimal(tr2.fee_jpy) > 0
    assert acc2.collateral_jpy < D(1_000_000)


def test_slippage_is_unfavourable_both_ways(acc, fxcfg):
    tr_long = open_pos(acc, fxcfg, LONG)
    assert Decimal(tr_long.price) > mk_board()["asks"][0][0]     # 買いは高く
    acc.position = None
    tr_short = open_pos(acc, fxcfg, SHORT)
    assert Decimal(tr_short.price) < mk_board()["bids"][0][0]    # 売りは安く


# --- 建玉管理料（スワップ） ----------------------------------------------

def test_swap_accrues_over_time(acc, fxcfg):
    open_pos(acc, fxcfg, LONG, notional=D(2_000_000))
    bid, ask = quotes()
    before = acc.collateral_jpy
    cost = acc.accrue_swap(BASE + 86400, bid, ask, fxcfg["fees"]["swap_rate_per_day"])
    # 想定元本約200万 × 0.04% = 約800円/日
    assert D(700) < cost < D(900)
    assert acc.collateral_jpy == before - cost
    assert acc.swap_paid == cost
    # 同じ時刻で二重課金しない
    assert acc.accrue_swap(BASE + 86400, bid, ask, fxcfg["fees"]["swap_rate_per_day"]) == 0


def test_swap_recorded_on_close(acc, fxcfg):
    open_pos(acc, fxcfg, LONG)
    bid, ask = quotes()
    acc.accrue_swap(BASE + 86400, bid, ask, fxcfg["fees"]["swap_rate_per_day"])
    tr = acc.close_position(ts="t", board=mk_board(), bid=bid, ask=ask, cfg=fxcfg,
                            signal_key="k", reason="r", now_ts=BASE + 86400, exit_kind="timeout")
    assert Decimal(tr.swap_jpy) > 0


def test_no_swap_without_position(acc, fxcfg):
    assert acc.accrue_swap(BASE + 86400, *quotes(), fxcfg["fees"]["swap_rate_per_day"]) == 0


# --- 強制ロスカット ------------------------------------------------------

def test_liquidation_triggered_on_margin_call(acc, fxcfg):
    open_pos(acc, fxcfg, LONG, notional=D(2_000_000))
    bid, ask = quotes()
    assert not acc.needs_liquidation(bid, ask, fxcfg)

    # 大きく下落させて維持率を割らせる
    crash = PRICE * D("0.6")
    bid2, ask2 = quotes(crash)
    assert acc.needs_liquidation(bid2, ask2, fxcfg)
    tr = acc.close_position(ts="t", board=mk_board(crash), bid=bid2, ask=ask2, cfg=fxcfg,
                            signal_key="k", reason="margin call", now_ts=BASE + 600,
                            exit_kind="liquidation")
    assert tr.exit_kind == "liquidation"
    assert acc.liquidations == 1
    assert acc.position is None


def test_short_liquidation_on_spike(acc, fxcfg):
    open_pos(acc, fxcfg, SHORT, notional=D(2_000_000))
    spike = PRICE * D("1.4")
    bid, ask = quotes(spike)
    assert acc.needs_liquidation(bid, ask, fxcfg)


def test_small_position_survives_drop(acc, fxcfg):
    """建玉が小さければ 5% 下落でもロスカットされない。"""
    open_pos(acc, fxcfg, LONG, notional=D(200_000))
    bid, ask = quotes(PRICE * D("0.95"))
    assert not acc.needs_liquidation(bid, ask, fxcfg)


# --- 戦略シグナル --------------------------------------------------------

UP = [100.0, 100.05, 100.1, 100.15, 100.6, 100.7, 100.8]
DOWN = [100.8, 100.7, 100.6, 100.15, 100.1, 99.9, 99.5]


def ctx(prices, acc_price=PRICE, spread=D("0.0003"), now_ts=BASE):
    bid, ask = quotes(acc_price)
    return {"ticks": mk_ticks(prices, now_ts), "best_bid": bid, "best_ask": ask,
            "spread_pct": spread, "now_ts": now_ts}


def test_f_goes_long_on_up_momentum(fxcfg, acc):
    s = strategy_f(fxcfg, {}, BASE, acc, ctx(UP))
    assert s.action == "OPEN" and s.side == LONG


def test_f_goes_short_on_down_momentum(fxcfg, acc):
    s = strategy_f(fxcfg, {}, BASE, acc, ctx(DOWN))
    assert s.action == "OPEN" and s.side == SHORT
    assert s.notional_jpy == D(200_000)


def test_f_no_entry_without_history(fxcfg, acc):
    s = strategy_f(fxcfg, {}, BASE, acc, ctx([100, 100.5, 101]))
    assert s.action == "HOLD" and "warmup" in s.reason


def test_f_no_entry_on_data_gap(fxcfg, acc):
    series = mk_ticks([100, 100.8])
    series[0]["ts"] = BASE - 7200
    series[0]["tick_id"] = str(BASE - 7200)
    bid, ask = quotes()
    s = strategy_f(fxcfg, {}, BASE, acc, {"ticks": series, "best_bid": bid, "best_ask": ask,
                                          "spread_pct": D("0.0003"), "now_ts": BASE})
    assert s.action == "HOLD" and "gap" in s.reason


def test_f_reverses_when_momentum_flips(fxcfg, acc):
    open_pos(acc, fxcfg, LONG)
    s = strategy_f(fxcfg, {}, BASE + 600, acc, ctx(DOWN, now_ts=BASE + 600))
    assert s.action == "REVERSE" and s.side == SHORT and s.exit_kind == "reversal"


def test_f_take_profit_on_short(fxcfg, acc):
    open_pos(acc, fxcfg, SHORT)
    lower = PRICE * D("0.98")                       # -2% → ショートは +2%
    s = strategy_f(fxcfg, {}, BASE + 600, acc, ctx(DOWN, acc_price=lower, now_ts=BASE + 600))
    assert s.action == "CLOSE" and s.exit_kind == "take_profit"


def test_f_stop_loss_on_short(fxcfg, acc):
    open_pos(acc, fxcfg, SHORT)
    higher = PRICE * D("1.01")                      # +1% → ショートは -1%
    s = strategy_f(fxcfg, {}, BASE + 600, acc, ctx(UP, acc_price=higher, now_ts=BASE + 600))
    assert s.action == "CLOSE" and s.exit_kind == "stop_loss"


def test_f_timeout(fxcfg, acc):
    open_pos(acc, fxcfg, LONG)
    s = strategy_f(fxcfg, {}, BASE + 7 * 3600, acc, ctx(UP, now_ts=BASE + 7 * 3600))
    assert s.action == "CLOSE" and s.exit_kind == "timeout"


def test_f_cooldown(fxcfg, acc):
    acc.last_exit_ts = BASE - 600
    s = strategy_f(fxcfg, {}, BASE, acc, ctx(UP))
    assert s.action == "HOLD" and "cooldown" in s.reason


def mk_bars(closes, tf="30m", start=0):
    step = 1800 if tf == "30m" else 3600
    return [{"ts": start + i * step, "o": str(c), "h": str(c), "l": str(c), "c": str(c), "v": "1"}
            for i, c in enumerate(closes)]


def test_g_shorts_on_dead_cross(fxcfg):
    acc = MarginAccount(strategy_id="G_ma_cross_ls", initial_jpy=D(1_000_000),
                        collateral_jpy=D(1_000_000))
    closes = [D(100 + 5 * i) for i in range(30)] + [D(245 - 20 * i) for i in range(1, 12)]
    found = []
    for n in range(31, len(closes) + 1):
        bars = {"30m": mk_bars(closes[:n])}
        now = bars["30m"][-1]["ts"] + 1800
        s = strategy_g(fxcfg, bars, now, acc, ctx(UP, now_ts=now))
        found.append((s.action, s.side))
    assert ("OPEN", SHORT) in found


def test_g_reverses_position(fxcfg):
    acc = MarginAccount(strategy_id="G_ma_cross_ls", initial_jpy=D(1_000_000),
                        collateral_jpy=D(1_000_000))
    closes = [D(100)] * 30 + [D(100 + 5 * i) for i in range(1, 10)]
    open_pos(acc, fxcfg, SHORT)
    reversed_ = False
    for n in range(31, len(closes) + 1):
        bars = {"30m": mk_bars(closes[:n])}
        now = bars["30m"][-1]["ts"] + 1800
        s = strategy_g(fxcfg, bars, now, acc, ctx(UP, now_ts=now))
        if s.action == "REVERSE" and s.side == LONG:
            reversed_ = True
    assert reversed_


def test_h_opens_once(fxcfg):
    acc = MarginAccount(strategy_id="H_long_hold_fx", initial_jpy=D(1_000_000),
                        collateral_jpy=D(1_000_000))
    s = strategy_h(fxcfg, {}, BASE, acc, ctx(UP))
    assert s.action == "OPEN" and s.side == LONG
    assert s.notional_jpy == D(2_000_000)          # 証拠金 × 2 倍
    open_pos(acc, fxcfg, LONG)
    assert strategy_h(fxcfg, {}, BASE, acc, ctx(UP)).action == "HOLD"


# --- 実行（run）レベル ---------------------------------------------------

@pytest.fixture()
def fxcfg_nb(fxcfg):
    fxcfg["data"]["warmup_bootstrap"] = False
    return fxcfg


@pytest.fixture()
def market(monkeypatch):
    from btcsim import api
    state = {"price": PRICE}

    def ticker(product, stats):
        assert product == "FX_BTC_JPY"
        p = state["price"]
        return {"ltp": p, "best_bid": p - D(2000), "best_ask": p + D(2000),
                "best_bid_size": D(1), "best_ask_size": D(1), "volume_24h": D(100),
                "exchange_timestamp": "2026-11-03T00:00:00.000"}

    monkeypatch.setattr(api, "get_ticker", ticker)
    monkeypatch.setattr(api, "get_board", lambda p, s, depth=30: mk_board(state["price"]))
    monkeypatch.setattr(api, "get_executions", lambda *a, **k: [])
    monkeypatch.setattr(api, "get_bitbank_1m", lambda *a, **k: [])
    return state


def test_fx_first_run_initializes(tmp_path, fxcfg_nb, market):
    assert fxrun.process_tick(tmp_path, fxcfg_nb) == 0
    st = json.loads((tmp_path / "state_fx" / "state.json").read_text(encoding="utf-8"))
    exp = st["experiment"]
    assert exp["product_code"] == "FX_BTC_JPY"
    assert exp["market"] == "margin"
    assert exp["leverage"] == "2"
    assert int(exp["ends_ts"]) - int(exp["started_ts"]) == 168 * 3600
    assert set(st["portfolios"]) == {"F_momentum_ls", "G_ma_cross_ls", "H_long_hold_fx"}
    # H はレバレッジ 2 倍でロングを建てる
    h = st["portfolios"]["H_long_hold_fx"]
    assert h["position"] is not None and h["position"]["side"] == LONG
    assert (tmp_path / "docs" / "fx-data.json").exists()
    assert (tmp_path / "docs" / "fx-report.html").exists()


def test_fx_uses_separate_state_dir(tmp_path, fxcfg_nb, market):
    """現物の state/ を一切作らない・触らないこと。"""
    fxrun.process_tick(tmp_path, fxcfg_nb)
    assert (tmp_path / "state_fx" / "state.json").exists()
    assert not (tmp_path / "state" / "state.json").exists()
    assert not (tmp_path / "docs" / "data.json").exists()      # 現物のダッシュボードを壊さない


def test_fx_idempotent(tmp_path, fxcfg_nb, market):
    fxrun.process_tick(tmp_path, fxcfg_nb)
    first = json.loads((tmp_path / "state_fx" / "state.json").read_text(encoding="utf-8"))
    fxrun.process_tick(tmp_path, fxcfg_nb)
    second = json.loads((tmp_path / "state_fx" / "state.json").read_text(encoding="utf-8"))
    assert first["portfolios"] == second["portfolios"]
    assert len(second["trades"]) == len(first["trades"])
    assert second["stats"]["skipped_duplicate_ticks"] == 1


def test_fx_liquidation_through_run(tmp_path, fxcfg_nb, market):
    """価格が崩れたら run 経由で強制ロスカットが発生する。"""
    fxrun.process_tick(tmp_path, fxcfg_nb)
    path = tmp_path / "state_fx" / "state.json"
    st = json.loads(path.read_text(encoding="utf-8"))
    assert st["portfolios"]["H_long_hold_fx"]["position"] is not None

    # 異常値判定に引っかからないよう、段階的に下げる
    for factor in ("0.88", "0.78", "0.69", "0.61", "0.55"):
        market["price"] = PRICE * D(factor)
        fxrun.process_tick(tmp_path, fxcfg_nb, force=True)
    st2 = json.loads(path.read_text(encoding="utf-8"))
    h = st2["portfolios"]["H_long_hold_fx"]
    assert h["liquidations"] >= 1
    assert h["position"] is None
    liq = [t for t in st2["trades"] if t.get("exit_kind") == "liquidation"]
    assert liq and liq[0]["strategy"] == "H_long_hold_fx"
    assert Decimal(h["collateral_jpy"]) < D(1_000_000)


def test_fx_api_error_skips_trading(tmp_path, fxcfg_nb, market, monkeypatch):
    from btcsim import api
    fxrun.process_tick(tmp_path, fxcfg_nb)
    before = json.loads((tmp_path / "state_fx" / "state.json").read_text(encoding="utf-8"))

    def boom(*a, **k):
        raise api.ApiError("down")

    monkeypatch.setattr(api, "get_ticker", boom)
    assert fxrun.process_tick(tmp_path, fxcfg_nb, force=True) == 0
    after = json.loads((tmp_path / "state_fx" / "state.json").read_text(encoding="utf-8"))
    assert after["portfolios"] == before["portfolios"]
    assert after["last_run"]["status"] == "api_error"


def test_fx_experiment_end_writes_report(tmp_path, fxcfg_nb, market):
    fxrun.process_tick(tmp_path, fxcfg_nb)
    path = tmp_path / "state_fx" / "state.json"
    st = json.loads(path.read_text(encoding="utf-8"))
    st["experiment"]["ends_ts"] = 1
    path.write_text(json.dumps(st), encoding="utf-8")

    assert fxrun.process_tick(tmp_path, fxcfg_nb, force=True) == 0
    st2 = json.loads(path.read_text(encoding="utf-8"))
    assert st2["experiment"]["status"] == "finished"
    for name in ("fx_final_report.md", "fx_final_report.json",
                 "fx_final_report.csv", "fx_final_report.html"):
        assert (tmp_path / "reports" / name).exists()
    md = (tmp_path / "reports" / "fx_final_report.md").read_text(encoding="utf-8")
    for needle in ("最終レポート", "仮想売買", "ショート", "ロスカット", "建玉管理料",
                   "損益分岐勝率", "空売り解禁の効果", "改善すべき点"):
        assert needle in md

    # 終了後は何もしない
    before = json.loads(path.read_text(encoding="utf-8"))
    assert fxrun.process_tick(tmp_path, fxcfg_nb, force=True) == 0
    assert json.loads(path.read_text(encoding="utf-8")) == before


def test_fx_dashboard_contract(tmp_path, fxcfg_nb, market):
    fxrun.process_tick(tmp_path, fxcfg_nb)
    d = json.loads((tmp_path / "docs" / "fx-data.json").read_text(encoding="utf-8"))
    assert {"experiment", "market_data", "strategies", "series", "trades",
            "spot_comparison", "stats"} <= set(d)
    assert {"leverage", "liquidation_margin_ratio_pct", "swap_rate_per_day_pct"} <= set(d["experiment"])
    for row in d["strategies"].values():
        for key in ("equity_jpy", "collateral_jpy", "position_side", "margin_ratio_pct",
                    "longs", "shorts", "liquidations", "swap_jpy", "total_cost_jpy",
                    "win_rate_pct", "by_side", "exit_breakdown", "last_reason"):
            assert key in row, key
    html = (tmp_path / "docs" / "fx-report.html").read_text(encoding="utf-8")
    assert "const EMBEDDED =" in html and "H_long_hold_fx" in html
    low = html.lower()
    for bad in ("api_key", "secret", "password", "token", "postgres://"):
        assert bad not in low


def test_fx_account_roundtrip(acc, fxcfg):
    open_pos(acc, fxcfg, SHORT)
    clone = MarginAccount.from_dict(acc.to_dict())
    assert clone.to_dict() == acc.to_dict()
    assert clone.pos().side == SHORT
    assert isinstance(clone.collateral_jpy, Decimal)
    # 未知フィールドは無視する（将来のスキーマ変更に耐える）
    MarginAccount.from_dict({**acc.to_dict(), "future": 1})


def test_fx_csv_export(tmp_path, fxcfg_nb, market):
    fxrun.process_tick(tmp_path, fxcfg_nb)
    s = Store(tmp_path, "state_fx")
    s.load()
    s.export_csv()
    text = (tmp_path / "state_fx" / "trades.csv").read_text(encoding="utf-8")
    assert "swap_jpy" in text.splitlines()[0]
    assert "H_long_hold_fx" in text


def test_no_private_api_in_fx_source():
    from pathlib import Path
    src = Path(__file__).resolve().parents[1] / "src" / "btcsim" / "fx"
    banned = ["sendchildorder", "cancelchildorder", "getpositions", "getcollateral",
              "ACCESS-KEY", "hmac"]
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for b in banned:
            assert b.lower() not in text, f"{path} に {b} が含まれています"


def test_fx_report_includes_spot_comparison(tmp_path, fxcfg_nb, market, monkeypatch):
    """FX レポートに現物比較セクションが入ること（ダッシュボードと同じ内容）。"""
    from btcsim import run as spot_run
    from btcsim.config import Config
    from btcsim.fx import report as fx_report

    spot_cfg = Config.load()
    spot_cfg["data"]["warmup_bootstrap"] = False

    # 現物側のティッカーは BTC_JPY を要求するのでスタブを差し替える
    from btcsim import api
    monkeypatch.setattr(api, "get_ticker", lambda p, s: {
        "ltp": PRICE, "best_bid": PRICE - D(2000), "best_ask": PRICE + D(2000),
        "best_bid_size": D(1), "best_ask_size": D(1), "volume_24h": D(100),
        "exchange_timestamp": "2026-11-03T00:00:00.000"})
    spot_run.process_tick(tmp_path, spot_cfg)          # 現物の state/ を作る
    fxrun.process_tick(tmp_path, fxcfg_nb)             # FX の state_fx/ を作る

    s = Store(tmp_path, "state_fx")
    s.load()
    paths = fx_report.build(tmp_path, s.state, fxcfg_nb)
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert "spot_comparison" in data
    md = paths["md"].read_text(encoding="utf-8")
    assert "空売り解禁の効果" in md
    # 比較できる場合はペア表が出る / できない場合も理由つきで出る
    assert ("対応する現物戦略" in md) or ("比較なし" in md)
