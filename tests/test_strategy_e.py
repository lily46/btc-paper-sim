"""戦略 E（短期モメンタム）と、途中追加に伴う互換性のテスト。"""
from __future__ import annotations

import json
from decimal import Decimal

import pytest

from btcsim import run, strategies, ticks as tickmod
from btcsim.portfolio import Portfolio
from btcsim.store import Store

D = Decimal
SID = "E_short_momentum"
BASE = 1_700_000_000          # 10 分境界


def mk_ticks(prices, now_ts=BASE, step_min=10):
    """[(分前, 価格)] ではなく「古い順の価格列」を 10 分刻みのティック列にする。"""
    n = len(prices)
    return [{"tick_id": str(now_ts - (n - 1 - i) * step_min * 60),
             "ts": now_ts - (n - 1 - i) * step_min * 60,
             "ltp": str(p), "best_bid": str(p), "best_ask": str(p)}
            for i, p in enumerate(prices)]


def ctx_for(prices, *, bid=None, spread=D("0.0001"), now_ts=BASE):
    series = mk_ticks(prices, now_ts)
    return {"ticks": series, "best_bid": D(str(bid if bid is not None else prices[-1])),
            "best_ask": D(str(prices[-1])), "spread_pct": spread, "now_ts": now_ts}


def fresh_pf():
    return Portfolio(strategy_id=SID, initial_jpy=D(1_000_000), jpy=D(1_000_000))


def sig_e(cfg, prices, pf=None, **kw):
    return strategies.strategy_e(cfg, {}, kw.pop("now_ts", BASE), pf or fresh_pf(),
                                 ctx_for(prices, now_ts=kw.pop("tick_now", BASE), **kw))


# --- ティック系列ヘルパー ------------------------------------------------

def test_change_pct_and_coverage():
    series = mk_ticks([100, 101, 102, 103])        # 30 分ぶん + 現在
    assert tickmod.covered_minutes(series, BASE) == 30.0
    assert tickmod.change_pct(series, BASE, 30) == (D(103) - D(100)) / D(100)
    assert tickmod.change_pct(series, BASE, 600) is None    # 基準が無い


def test_price_at_ignores_future_ticks():
    series = mk_ticks([100, 200, 300])
    # 20 分前を要求したら 20 分前の値（未来の 300 を拾わない）
    assert tickmod.price_at(series, BASE - 20 * 60, 420) == D(100)


def test_max_gap_detects_missing_ticks():
    series = mk_ticks([100, 101, 102, 103, 104, 105, 106])   # 60 分ぶん、欠けなし
    assert tickmod.max_gap_minutes(series, BASE, 60) <= 10
    holed = [series[0], series[-1]]                           # 途中が全部欠けている
    assert tickmod.max_gap_minutes(holed, BASE, 60) >= 50


def test_tick_ts_falls_back_to_tick_id():
    assert tickmod.tick_ts({"tick_id": "123"}) == 123         # 旧データ（ts なし）


# --- エントリー条件 ------------------------------------------------------

UP = [100.0, 100.05, 100.1, 100.15, 100.6, 100.7, 100.8]      # 60分+0.8% / 30分+0.65%


def test_entry_on_momentum(cfg):
    s = sig_e(cfg, UP)
    assert s.action == "BUY"
    assert s.order_jpy == D(100_000)
    assert "momentum" in s.reason


def test_no_entry_when_short_momentum_too_small(cfg):
    flat = [100, 100, 100, 100, 100, 100, 100.1]              # 30分 +0.1% < 0.25%
    s = sig_e(cfg, flat)
    assert s.action == "HOLD" and "momentum" in s.reason


def test_no_entry_when_long_change_negative(cfg):
    # 30 分は +0.5% だが 60 分では -0.5%（急落からの小戻し）
    prices = [101, 100.9, 100.0, 99.6, 100.0, 100.3, 100.5]
    s = sig_e(cfg, prices)
    assert s.action == "HOLD" and "not positive" in s.reason


def test_no_entry_when_spread_too_wide(cfg):
    s = sig_e(cfg, UP, spread=D("0.01"))
    assert s.action == "HOLD" and "spread" in s.reason


def test_no_entry_without_enough_history(cfg):
    s = sig_e(cfg, [100, 100.5, 101])                          # 20 分ぶんしかない
    assert s.action == "HOLD" and "warmup" in s.reason


def test_no_entry_when_data_has_gap(cfg):
    series = mk_ticks([100, 100.8])                            # 10 分刻みでなく 10 分だけ
    series[0]["ts"] = BASE - 7200                              # 2 時間前 → 大穴
    series[0]["tick_id"] = str(BASE - 7200)
    ctx = {"ticks": series, "best_bid": D("100.8"), "best_ask": D("100.8"),
           "spread_pct": D("0.0001"), "now_ts": BASE}
    s = strategies.strategy_e(cfg, {}, BASE, fresh_pf(), ctx)
    assert s.action == "HOLD" and "gap" in s.reason


def test_cooldown_blocks_reentry(cfg):
    pf = fresh_pf()
    pf.last_exit_ts = BASE - 10 * 60                           # 10 分前に決済
    s = sig_e(cfg, UP, pf=pf)
    assert s.action == "HOLD" and "cooldown" in s.reason
    pf.last_exit_ts = BASE - 31 * 60                           # 31 分前なら解除
    assert sig_e(cfg, UP, pf=pf).action == "BUY"


def test_entry_key_is_stable_within_the_same_tick(cfg):
    assert sig_e(cfg, UP).key == sig_e(cfg, UP).key            # 重複発注されない


# --- エグジット条件 ------------------------------------------------------

def held_pf(entry_price, entry_ts=BASE - 60 * 60, btc="0.008"):
    pf = fresh_pf()
    pf.btc = D(btc)
    pf.jpy = D(900_000)
    pf.cost_basis_jpy = D(entry_price) * D(btc)
    pf.position_entry_price = str(entry_price)
    pf.position_entry_ts = entry_ts
    pf.position_entry_reason = "test entry"
    return pf


def test_take_profit(cfg):
    pf = held_pf(100)
    s = sig_e(cfg, UP, pf=pf, bid=100.9)                       # +0.9% >= 0.8%
    assert s.action == "SELL" and s.exit_kind == "take_profit"


def test_stop_loss(cfg):
    pf = held_pf(100)
    s = sig_e(cfg, UP, pf=pf, bid=99.4)                        # -0.6% <= -0.5%
    assert s.action == "SELL" and s.exit_kind == "stop_loss"


def test_timeout_exit(cfg):
    pf = held_pf(100, entry_ts=BASE - 7 * 3600)                # 7 時間保有 > 6 時間
    s = sig_e(cfg, UP, pf=pf, bid=100.1)
    assert s.action == "SELL" and s.exit_kind == "timeout"


def test_trend_reversal_exit(cfg):
    down = [100, 100, 100, 100, 99.9, 99.8, 99.6]              # 30分 -0.4% <= -0.25%
    pf = held_pf(100)
    s = sig_e(cfg, down, pf=pf, bid=99.7)
    assert s.action == "SELL" and s.exit_kind == "trend_reversal"


def test_holds_inside_thresholds(cfg):
    pf = held_pf(100)
    s = sig_e(cfg, UP, pf=pf, bid=100.3)                       # +0.3%: 利確も損切りも未達
    assert s.action == "HOLD" and "holding" in s.reason


def test_exit_checked_before_entry(cfg):
    """保有中はエントリー条件を見ない（ピラミッディングしない）。"""
    pf = held_pf(100)
    s = sig_e(cfg, UP, pf=pf, bid=100.2)
    assert s.action != "BUY"


# --- 実行（run）レベル ---------------------------------------------------

@pytest.fixture()
def cfg_nb(cfg):
    cfg["data"]["warmup_bootstrap"] = False
    return cfg


@pytest.fixture()
def market(monkeypatch):
    from btcsim import api
    state = {"price": D(10_000_000)}

    def ticker(product, stats):
        p = state["price"]
        return {"ltp": p, "best_bid": p - D(2000), "best_ask": p + D(2000),
                "best_bid_size": D(1), "best_ask_size": D(1), "volume_24h": D(100),
                "exchange_timestamp": "2026-11-03T00:00:00.000"}

    def board(product, stats, depth=30):
        p = state["price"]
        return {"asks": [(p + D(2000) + D(1000) * i, D(2)) for i in range(30)],
                "bids": [(p - D(2000) - D(1000) * i, D(2)) for i in range(30)]}

    monkeypatch.setattr(api, "get_ticker", ticker)
    monkeypatch.setattr(api, "get_board", board)
    monkeypatch.setattr(api, "get_executions", lambda *a, **k: [])
    monkeypatch.setattr(api, "get_bitbank_1m", lambda *a, **k: [])
    return state


def legacy_state(tmp_path, cfg):
    """戦略 E が存在しない「旧バージョン」の state を作る。"""
    cfg2 = dict(cfg)
    cfg2["strategies"] = {k: v for k, v in cfg["strategies"].items() if k != SID}
    from btcsim.config import Config
    old_cfg = Config(cfg2)
    run.process_tick(tmp_path, old_cfg)
    path = tmp_path / "state" / "state.json"
    st = json.loads(path.read_text(encoding="utf-8"))
    st.pop("strategy_windows", None)            # 旧データには存在しないキー
    for pf in st["portfolios"].values():         # 旧データには無いフィールドも削る
        for k in ("position_entry_ts", "position_entry_price", "position_entry_reason",
                  "last_exit_ts", "hold_minutes_total"):
            pf.pop(k, None)
    for t in st["ticks"]:
        t.pop("ts", None)
    path.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
    return st


def test_legacy_state_loads_and_adds_e_once(tmp_path, cfg_nb, market):
    """E が無い旧 state を読み、E だけが追加され A〜D は一切変わらないこと。"""
    before = legacy_state(tmp_path, cfg_nb)
    assert SID not in before["portfolios"]

    assert run.process_tick(tmp_path, cfg_nb, force=True) == 0
    st = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))

    # A〜D の残高・開始時刻・終了時刻は不変
    assert st["experiment"]["started_ts"] == before["experiment"]["started_ts"]
    assert st["experiment"]["ends_ts"] == before["experiment"]["ends_ts"]
    for sid, pf in before["portfolios"].items():
        for key in ("jpy", "btc", "trades", "realized_pnl", "fees_paid"):
            assert st["portfolios"][sid][key] == pf[key], f"{sid}.{key} が変わった"
    assert len(st["trades"]) == len(before["trades"])

    # E は独立した資金と独立した期間を持つ
    e = st["portfolios"][SID]
    assert Decimal(e["jpy"]) == Decimal("1000000") and Decimal(e["btc"]) == 0
    win = st["strategy_windows"][SID]
    assert win["inherited"] is False
    assert int(win["ends_ts"]) - int(win["started_ts"]) == 168 * 3600
    assert int(win["started_ts"]) >= int(before["experiment"]["started_ts"])
    assert int(win["ends_ts"]) >= int(before["experiment"]["ends_ts"])   # E の方が後に終わる
    # A〜D の window は全体期間をそのまま引き継ぐ
    for sid in before["portfolios"]:
        assert st["strategy_windows"][sid]["inherited"] is True
        assert st["strategy_windows"][sid]["ends_ts"] == before["experiment"]["ends_ts"]

    # 2 回目の実行で E が作り直されない（初期化は一度だけ）
    started = win["started_ts"]
    run.process_tick(tmp_path, cfg_nb, force=True)
    st2 = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    assert st2["strategy_windows"][SID]["started_ts"] == started


def test_e_window_independent_from_core(tmp_path, cfg_nb, market):
    """A〜D が終わっても E が期間内なら実験は続く。"""
    run.process_tick(tmp_path, cfg_nb)
    path = tmp_path / "state" / "state.json"
    st = json.loads(path.read_text(encoding="utf-8"))
    st["experiment"]["ends_ts"] = 1
    for sid, win in st["strategy_windows"].items():
        if sid != SID:
            win["ends_ts"] = 1              # A〜D だけ終了
    path.write_text(json.dumps(st), encoding="utf-8")

    assert run.process_tick(tmp_path, cfg_nb, force=True) == 0
    st2 = json.loads(path.read_text(encoding="utf-8"))
    assert st2["experiment"]["status"] == "running"          # 全体は終了しない
    assert st2["strategy_windows"][SID]["status"] == "running"
    for sid in st2["strategy_windows"]:
        if sid != SID:
            assert st2["strategy_windows"][sid]["status"] == "finished"
            assert st2["strategy_windows"][sid]["final"] is not None
            assert st2["last_run"]["decisions"][sid]["reason"] == "period finished; no new trades"
    assert len(st2["ticks"]) == 2                            # 市場データ取得は続く

    # E も終われば全体終了＆最終レポート
    st2["strategy_windows"][SID]["ends_ts"] = 1
    path.write_text(json.dumps(st2), encoding="utf-8")
    assert run.process_tick(tmp_path, cfg_nb, force=True) == 0
    st3 = json.loads(path.read_text(encoding="utf-8"))
    assert st3["experiment"]["status"] == "finished"
    assert (tmp_path / "reports" / "final_report.md").exists()


def test_e_buys_with_fixed_notional_and_records_details(tmp_path, cfg_nb, market, monkeypatch):
    """E の購入額が 100,000 円で、スプレッド等の詳細が記録されること。"""
    run.process_tick(tmp_path, cfg_nb)
    path = tmp_path / "state" / "state.json"

    # 強い上昇のティック履歴を仕込む（E のエントリー条件を満たす）
    st = json.loads(path.read_text(encoding="utf-8"))
    base = int(st["ticks"][-1]["ts"])
    px = [9_900_000, 9_910_000, 9_920_000, 9_930_000, 9_980_000, 9_990_000]
    st["ticks"] = [{"tick_id": str(base - (len(px) - i) * 600), "ts": base - (len(px) - i) * 600,
                    "ltp": str(p), "best_bid": str(p), "best_ask": str(p),
                    "best_bid_size": "1", "best_ask_size": "1",
                    "fetched_at_jst": "x", "fetched_at_utc": "x",
                    "exchange_timestamp": "x", "source": "test"} for i, p in enumerate(px)]
    path.write_text(json.dumps(st), encoding="utf-8")

    assert run.process_tick(tmp_path, cfg_nb, force=True) == 0
    st2 = json.loads(path.read_text(encoding="utf-8"))
    e_trades = [t for t in st2["trades"] if t["strategy"] == SID]
    assert len(e_trades) == 1
    tr = e_trades[0]
    assert tr["side"] == "BUY"
    assert Decimal(tr["gross_jpy"]) <= Decimal("100000")      # 1 回の購入上限
    assert Decimal(tr["gross_jpy"]) > Decimal("95000")
    assert Decimal(tr["fee_jpy"]) == (Decimal(tr["gross_jpy"]) * Decimal("0.0015")).quantize(Decimal("0.0001"))
    assert tr["spread_pct"] and Decimal(tr["slippage_pct"]) > 0
    pf = st2["portfolios"][SID]
    assert Decimal(pf["btc"]) > 0
    assert pf["position_entry_price"] == tr["price"]
    assert pf["position_entry_ts"] is not None
    # 同じティックを再処理しても二重発注されない
    before_trades = len(st2["trades"])
    run.process_tick(tmp_path, cfg_nb)
    st3 = json.loads(path.read_text(encoding="utf-8"))
    assert len(st3["trades"]) == before_trades


def test_e_full_round_trip_records_hold_time(tmp_path, cfg_nb, market):
    """買い → 利確売り まで通して、保有時間とエグジット理由が残ること。"""
    run.process_tick(tmp_path, cfg_nb)
    path = tmp_path / "state" / "state.json"
    st = json.loads(path.read_text(encoding="utf-8"))

    pf = Portfolio.from_dict(st["portfolios"][SID])
    entry_ts = int(st["ticks"][-1]["ts"]) - 3600
    pf.btc = D("0.01")
    pf.jpy = D(900_000)
    pf.cost_basis_jpy = D("0.01") * D(9_000_000)
    pf.position_entry_price = "9000000"       # 現在値 1,000万 → +11% で利確
    pf.position_entry_ts = entry_ts
    pf.position_entry_reason = "test entry"
    st["portfolios"][SID] = pf.to_dict()
    path.write_text(json.dumps(st), encoding="utf-8")

    assert run.process_tick(tmp_path, cfg_nb, force=True) == 0
    st2 = json.loads(path.read_text(encoding="utf-8"))
    sells = [t for t in st2["trades"] if t["strategy"] == SID and t["side"] == "SELL"]
    assert len(sells) == 1
    tr = sells[0]
    assert tr["exit_kind"] == "take_profit"
    assert Decimal(tr["hold_minutes"]) == pytest.approx(60, abs=1)
    assert Decimal(tr["realized_pnl"]) > 0
    assert tr["entry_price"] == "9000000"
    assert tr["entry_reason"] == "test entry"

    pf2 = Portfolio.from_dict(st2["portfolios"][SID])
    assert pf2.btc == 0
    assert pf2.position_entry_ts is None          # 建玉情報がクリアされる
    assert pf2.last_exit_ts is not None           # クールダウンの基点が入る
    assert pf2.avg_hold_minutes() == pytest.approx(60, abs=1)
    assert pf2.wins == 1

    # 決済直後はクールダウンで買い直さない
    assert run.process_tick(tmp_path, cfg_nb, force=True) == 0
    st3 = json.loads(path.read_text(encoding="utf-8"))
    assert st3["last_run"]["decisions"][SID]["executed"] is False


def test_csv_export_handles_mixed_trade_shapes(tmp_path, cfg_nb, market):
    """旧形式（列が少ない）と新形式の取引が混在しても CSV が出せること。"""
    run.process_tick(tmp_path, cfg_nb)
    s = Store(tmp_path)
    s.load()
    s.state["trades"].insert(0, {"ts": "old", "strategy": "A_ma_cross", "side": "BUY",
                                 "price": "1", "size": "1"})     # 旧形式（列が少ない）
    s.export_csv()
    text = (tmp_path / "state" / "trades.csv").read_text(encoding="utf-8")
    assert "exit_kind" in text.splitlines()[0]
    assert "old" in text


def test_dashboard_and_report_include_e(tmp_path, cfg_nb, market):
    from btcsim import report
    run.process_tick(tmp_path, cfg_nb)
    s = Store(tmp_path)
    s.load()
    d = json.loads((tmp_path / "docs" / "data.json").read_text(encoding="utf-8"))
    assert SID in d["strategies"]
    row = d["strategies"][SID]
    for key in ("equity_jpy", "unrealized_pnl_jpy", "realized_pnl_jpy", "btc_balance",
                "trades", "win_rate_pct", "fees_jpy", "avg_hold_minutes",
                "max_drawdown_pct", "remaining_human", "last_reason", "exit_breakdown",
                "breakeven_win_rate_pct"):
        assert key in row, key
    assert SID in d["series"]["equity"]
    assert "common_period" in d

    md = report.build(tmp_path, s.state, cfg_nb)["md"].read_text(encoding="utf-8")
    assert "戦略 E（短期モメンタム）の売買分析" in md
    assert "共通期間での比較" in md
    assert "損益分岐勝率" in md
    assert "1 日あたり平均" in md
    assert "戦略ごとの実験期間" in md


def test_e_respects_drawdown_halt(tmp_path, cfg_nb, market):
    run.process_tick(tmp_path, cfg_nb)
    path = tmp_path / "state" / "state.json"
    st = json.loads(path.read_text(encoding="utf-8"))
    st["portfolios"][SID]["halted"] = True
    st["portfolios"][SID]["halt_reason"] = "test halt"
    base = int(st["ticks"][-1]["ts"])
    px = [9_900_000, 9_910_000, 9_920_000, 9_930_000, 9_980_000, 9_990_000]
    st["ticks"] = [{"tick_id": str(base - (len(px) - i) * 600), "ts": base - (len(px) - i) * 600,
                    "ltp": str(p), "best_bid": str(p), "best_ask": str(p),
                    "best_bid_size": "1", "best_ask_size": "1", "fetched_at_jst": "x",
                    "fetched_at_utc": "x", "exchange_timestamp": "x", "source": "test"}
                   for i, p in enumerate(px)]
    path.write_text(json.dumps(st), encoding="utf-8")

    run.process_tick(tmp_path, cfg_nb, force=True)
    st2 = json.loads(path.read_text(encoding="utf-8"))
    assert [t for t in st2["trades"] if t["strategy"] == SID] == []
    assert "halted" in st2["last_run"]["decisions"][SID]["reason"]
