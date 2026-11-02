from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from btcsim import api, report, run
from btcsim.store import Store, StoreError

D = Decimal
PRICE = D(10_000_000)


@pytest.fixture()
def fake_market(monkeypatch):
    """bitFlyer API をスタブ化（テストはネットワークに接触しない）。"""
    state = {"price": PRICE}

    def ticker(product, stats):
        p = state["price"]
        return {"ltp": p, "best_bid": p - D(2000), "best_ask": p + D(2000),
                "best_bid_size": D(1), "best_ask_size": D(1), "volume_24h": D(100),
                "exchange_timestamp": "2026-11-02T00:00:00.000"}

    def board(product, stats, depth=30):
        p = state["price"]
        return {"asks": [(p + D(2000) + D(1000) * i, D(2)) for i in range(30)],
                "bids": [(p - D(2000) - D(1000) * i, D(2)) for i in range(30)]}

    monkeypatch.setattr(api, "get_ticker", ticker)
    monkeypatch.setattr(api, "get_board", board)
    monkeypatch.setattr(api, "get_executions", lambda *a, **k: [])
    monkeypatch.setattr(api, "get_bitbank_1m", lambda *a, **k: [])
    return state


@pytest.fixture()
def cfg_nb(cfg):
    cfg["data"]["warmup_bootstrap"] = False
    return cfg


# --- Store ---------------------------------------------------------------

def test_transaction_rolls_back_on_error(tmp_path):
    s = Store(tmp_path)
    s.load()
    with s.transaction() as st:
        st["stats"]["runs"] = 1
    assert json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))["stats"]["runs"] == 1
    with pytest.raises(RuntimeError):
        with s.transaction() as st:
            st["stats"]["runs"] = 99
            raise RuntimeError("boom")
    on_disk = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    assert on_disk["stats"]["runs"] == 1          # 部分更新されていない
    assert s.state["stats"]["runs"] == 1          # メモリ側もロールバック


def test_corrupt_state_raises(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(StoreError):
        Store(tmp_path).load()


def test_schema_mismatch_raises(tmp_path):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text('{"schema": 999}', encoding="utf-8")
    with pytest.raises(StoreError):
        Store(tmp_path).load()


def test_db_failure_aborts_trading(tmp_path, cfg_nb, fake_market):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "state.json").write_text("broken", encoding="utf-8")
    assert run.process_tick(tmp_path, cfg_nb) == 2      # 売買せず異常終了


def test_tick_idempotency_markers(tmp_path):
    s = Store(tmp_path)
    s.load()
    assert not s.is_tick_processed("1")
    s.mark_tick_processed("1")
    s.mark_tick_processed("1")
    assert s.is_tick_processed("1")
    assert s.state["processed_tick_ids"].count("1") == 1


# --- run / 統合 -----------------------------------------------------------

def test_first_run_initializes_and_buys_bh(tmp_path, cfg_nb, fake_market):
    assert run.process_tick(tmp_path, cfg_nb) == 0
    st = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    exp = st["experiment"]
    assert exp["status"] == "running"
    assert int(exp["ends_ts"]) - int(exp["started_ts"]) == 168 * 3600
    assert set(st["portfolios"]) == {"A_ma_cross", "B_rsi_reversal", "C_combo", "D_buy_and_hold"}
    bh = st["portfolios"]["D_buy_and_hold"]
    assert Decimal(bh["btc"]) > 0
    assert Decimal(bh["jpy"]) < Decimal("1000000")
    # ウォームアップ中なので A/B/C は売買しない
    for sid in ("A_ma_cross", "B_rsi_reversal", "C_combo"):
        assert st["portfolios"][sid]["trades"] == 0
        assert Decimal(st["portfolios"][sid]["jpy"]) == Decimal("1000000")
    assert (tmp_path / "docs" / "data.json").exists()


def test_rerun_same_tick_does_not_double_trade(tmp_path, cfg_nb, fake_market):
    run.process_tick(tmp_path, cfg_nb)
    first = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    run.process_tick(tmp_path, cfg_nb)      # 同一 10 分枠の再試行
    second = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    assert first["portfolios"] == second["portfolios"]
    assert len(second["trades"]) == len(first["trades"])
    assert second["stats"]["skipped_duplicate_ticks"] == 1


def test_resume_does_not_reset_capital(tmp_path, cfg_nb, fake_market):
    run.process_tick(tmp_path, cfg_nb)
    st_path = tmp_path / "state" / "state.json"
    st = json.loads(st_path.read_text(encoding="utf-8"))
    started = st["experiment"]["started_ts"]
    st["portfolios"]["A_ma_cross"]["jpy"] = "777777"
    st_path.write_text(json.dumps(st), encoding="utf-8")

    run.process_tick(tmp_path, cfg_nb, force=True)       # 再起動相当
    st2 = json.loads(st_path.read_text(encoding="utf-8"))
    assert st2["experiment"]["started_ts"] == started     # 実験開始時刻を上書きしない
    assert Decimal(st2["portfolios"]["A_ma_cross"]["jpy"]) == Decimal("777777")
    assert st2["portfolios"]["D_buy_and_hold"]["buys"] == 1   # B&H は買い増さない


def test_api_error_skips_trading(tmp_path, cfg_nb, fake_market, monkeypatch):
    run.process_tick(tmp_path, cfg_nb)
    before = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))

    def boom(*a, **k):
        raise api.ApiError("network down")

    monkeypatch.setattr(api, "get_ticker", boom)
    assert run.process_tick(tmp_path, cfg_nb, force=True) == 0
    after = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    assert after["portfolios"] == before["portfolios"]
    assert after["last_run"]["status"] == "api_error"
    assert len(after["ticks"]) == len(before["ticks"])


def test_abnormal_price_jump_rejected(tmp_path, cfg_nb, fake_market):
    run.process_tick(tmp_path, cfg_nb)
    fake_market["price"] = PRICE * D(3)        # 異常値
    assert run.process_tick(tmp_path, cfg_nb, force=True) == 0
    st = json.loads((tmp_path / "state" / "state.json").read_text(encoding="utf-8"))
    assert st["last_run"]["status"] == "rejected"
    assert st["stats"]["data_rejections"] == 1
    assert len(st["ticks"]) == 1               # 異常ティックは記録しない


def test_experiment_end_stops_trading_and_writes_report(tmp_path, cfg_nb, fake_market):
    run.process_tick(tmp_path, cfg_nb)
    st_path = tmp_path / "state" / "state.json"
    st = json.loads(st_path.read_text(encoding="utf-8"))
    st["experiment"]["ends_ts"] = 1            # 既に終了済み扱い
    st_path.write_text(json.dumps(st), encoding="utf-8")

    assert run.process_tick(tmp_path, cfg_nb, force=True) == 0
    st2 = json.loads(st_path.read_text(encoding="utf-8"))
    assert st2["experiment"]["status"] == "finished"
    for name in ("final_report.md", "final_report.json", "final_report.csv"):
        assert (tmp_path / "reports" / name).exists()

    # 終了後はデータ取得も売買もしない（＝再スタートしない）
    before = json.loads(st_path.read_text(encoding="utf-8"))
    assert run.process_tick(tmp_path, cfg_nb, force=True) == 0
    assert json.loads(st_path.read_text(encoding="utf-8")) == before


def test_report_contains_required_sections(tmp_path, cfg_nb, fake_market):
    run.process_tick(tmp_path, cfg_nb)
    s = Store(tmp_path)
    s.load()
    paths = report.build(tmp_path, s.state, cfg_nb)
    md = paths["md"].read_text(encoding="utf-8")
    for needle in ("最終レポート", "仮想売買", "勝率の算出方法", "最大DD", "API エラー回数", "改善すべき点",
                   "スケジュール実行漏れ", "D_buy_and_hold"):
        assert needle in md
    data = json.loads(paths["json"].read_text(encoding="utf-8"))
    assert data["paper_trading_only"] is True


def test_dashboard_has_no_secrets(tmp_path, cfg_nb, fake_market):
    run.process_tick(tmp_path, cfg_nb)
    raw = (tmp_path / "docs" / "data.json").read_text(encoding="utf-8").lower()
    for bad in ("api_key", "apikey", "secret", "postgres://", "password", "token"):
        assert bad not in raw
    assert "仮想売買" in (tmp_path / "docs" / "data.json").read_text(encoding="utf-8")


def test_csv_exports(tmp_path, cfg_nb, fake_market):
    run.process_tick(tmp_path, cfg_nb)
    assert (tmp_path / "state" / "market.csv").exists()
    assert (tmp_path / "state" / "trades.csv").exists()
    trades = (tmp_path / "state" / "trades.csv").read_text(encoding="utf-8")
    assert "D_buy_and_hold" in trades


def test_no_private_api_endpoints_in_source():
    """実注文系のエンドポイントがソースに存在しないことを機械的に検査する。"""
    src = Path(__file__).resolve().parents[1] / "src"
    banned = ["sendchildorder", "cancelchildorder", "getbalance", "me/", "ACCESS-KEY", "hmac"]
    for path in src.rglob("*.py"):
        text = path.read_text(encoding="utf-8").lower()
        for b in banned:
            assert b.lower() not in text, f"{path} に {b} が含まれています"


def test_dashboard_data_contract(tmp_path, cfg_nb, fake_market):
    """docs/index.html が参照するキーが data.json に揃っているか（表示項目の欠落防止）。"""
    run.process_tick(tmp_path, cfg_nb)
    d = json.loads((tmp_path / "docs" / "data.json").read_text(encoding="utf-8"))
    assert {"disclaimer", "experiment", "market", "strategies", "stats", "series", "trades"} <= set(d)
    assert {"started_at_jst", "ends_at_jst", "remaining_human", "finished_at_jst", "status"} <= set(d["experiment"])
    assert {"ltp", "best_bid", "best_ask", "price_change_pct", "last_fetch_jst"} <= set(d["market"])
    assert {"ts", "price", "equity"} <= set(d["series"])
    for row in d["strategies"].values():
        assert {"equity_jpy", "jpy_balance", "btc_balance", "unrealized_pnl_jpy",
                "realized_pnl_jpy", "total_pnl_jpy", "total_pnl_pct", "trades",
                "fees_jpy", "max_drawdown_pct", "win_rate_pct",
                "vs_buy_and_hold_jpy"} <= set(row)
    assert {"runs", "observed_ticks", "expected_ticks", "missed_schedules",
            "api_errors", "data_rejections"} <= set(d["stats"])


def test_git_sync_skips_missing_paths(tmp_path, monkeypatch):
    """存在しないディレクトリを git add しようとして落ちないこと。"""
    s = Store(tmp_path)
    s.load()
    with s.transaction():
        pass
    calls = []

    class FakeProc:
        returncode = 0
        stderr = ""
        stdout = ""

    def fake_run(args, **kwargs):
        calls.append(args)
        if args[1] == "diff":
            return type("P", (), {"returncode": 1, "stderr": "", "stdout": ""})()
        return FakeProc()

    import subprocess as sp
    monkeypatch.setattr(sp, "run", fake_run)
    assert s.git_sync("msg") is True
    added = [a for a in calls if a[1] == "add"]
    assert added and "state" in added[0] and "reports" not in added[0]


def test_git_sync_raises_when_push_fails(tmp_path, monkeypatch):
    from btcsim.store import GitSyncError
    s = Store(tmp_path)
    s.load()
    with s.transaction():
        pass

    def fake_run(args, **kwargs):
        rc = 0
        if args[1] in ("push", "diff"):
            rc = 1
        return type("P", (), {"returncode": rc, "stderr": "", "stdout": ""})()

    import subprocess as sp
    monkeypatch.setattr(sp, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    with pytest.raises(GitSyncError):
        s.git_sync("msg", attempts=2)


def test_standalone_report_html_is_self_contained(tmp_path, cfg_nb, fake_market):
    """docs/report.html がデータ埋め込み済みで、file:// でも開けること。"""
    run.process_tick(tmp_path, cfg_nb)
    html = (tmp_path / "docs" / "report.html").read_text(encoding="utf-8")
    assert "const EMBEDDED =" in html
    assert '"D_buy_and_hold"' in html          # データが埋め込まれている
    assert "const EMBEDDED = /*__EMBEDDED_DATA__*/ null" not in html
    assert "仮想売買" in html
    # 埋め込み JSON を取り出して壊れていないか検証
    start = html.index("/*__EMBEDDED_DATA__*/") + len("/*__EMBEDDED_DATA__*/")
    end = html.index("/*__END_EMBEDDED_DATA__*/")
    data = json.loads(html[start:end].strip().replace("<\/", "</"))
    assert data["strategies"]["D_buy_and_hold"]["trades"] == 1
    # 認証情報が混入していないこと
    low = html.lower()
    for bad in ("api_key", "apikey", "secret", "postgres://", "password"):
        assert bad not in low


def test_final_report_includes_html(tmp_path, cfg_nb, fake_market):
    run.process_tick(tmp_path, cfg_nb)
    s = Store(tmp_path)
    s.load()
    paths = report.build(tmp_path, s.state, cfg_nb)
    assert paths["html"] is not None and paths["html"].exists()
    html = paths["html"].read_text(encoding="utf-8")
    assert "const EMBEDDED =" in html and '"D_buy_and_hold"' in html
