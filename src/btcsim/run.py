"""1 ティック分の処理（GitHub Actions から 10 分ごとに起動されるエントリポイント）。

処理順:
  状態ロード -> 冪等チェック -> 実験初期化/再開 -> 終了判定 -> 市場データ取得
  -> 異常値チェック -> ローソク足更新 -> 指標/シグナル -> 仮想売買 -> 保存 -> 公開物更新
実注文は一切行わない（Private API を呼ぶコードが存在しない）。
"""
from __future__ import annotations

import argparse
import logging
import sys
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from . import api, candles, dashboard, report, strategies
from .api import ApiError, ApiStats
from .config import Config
from .portfolio import Portfolio
from .store import GitSyncError, Store, StoreError
from .tzutil import get_tz

log = logging.getLogger("btcsim")


def _parse_exec_date(s: str) -> int:
    """bitFlyer の exec_date（UTC, 'Z' なしのことがある）を epoch 秒にする。"""
    t = s.replace("Z", "")
    if "." in t:
        head, frac = t.split(".", 1)
        t = head + "." + frac[:6]
    dt = datetime.fromisoformat(t).replace(tzinfo=timezone.utc)
    return int(dt.timestamp())


def _jst(ts: int, tz: str) -> str:
    return datetime.fromtimestamp(ts, get_tz(tz)).isoformat(timespec="seconds")


def init_experiment(state: dict, cfg: Config, now_ts: int) -> None:
    """初回起動時のみ呼ばれる。以降は決して初期資金を再設定しない。"""
    exp = cfg["experiment"]
    tz = exp["timezone"]
    ends = now_ts + int(exp["duration_hours"]) * 3600
    state["experiment"] = {
        "started_ts": now_ts,
        "started_at_jst": _jst(now_ts, tz),
        "ends_ts": ends,
        "ends_at_jst": _jst(ends, tz),
        "duration_hours": int(exp["duration_hours"]),
        "initial_capital_jpy": str(exp["initial_capital_jpy"]),
        "product_code": exp["product_code"],
        "timezone": tz,
        "status": "running",
        "finished_at_jst": None,
        "start_btc_price": None,
    }
    state["portfolios"] = {
        sid: Portfolio(strategy_id=sid, initial_jpy=exp["initial_capital_jpy"],
                       jpy=exp["initial_capital_jpy"]).to_dict()
        for sid in cfg.strategy_ids
    }
    log.info("実験を初期化: 開始 %s / 終了予定 %s / 戦略 %s",
             state["experiment"]["started_at_jst"], state["experiment"]["ends_at_jst"],
             ", ".join(state["portfolios"]))


def sanity_check(cfg: Config, ticker: dict, last_price: Decimal | None) -> str | None:
    s = cfg["sanity"]
    bid, ask, ltp = ticker["best_bid"], ticker["best_ask"], ticker["ltp"]
    if min(bid, ask, ltp) <= 0:
        return "非正の価格"
    if not (s["min_price_jpy"] <= ltp <= s["max_price_jpy"]):
        return f"価格が想定レンジ外: {ltp}"
    if ask <= bid:
        return f"板が異常 (bid={bid} >= ask={ask})"
    if (ask - bid) / bid > s["max_spread_pct"]:
        return f"スプレッド過大: {(ask - bid) / bid:.4%}"
    if last_price and last_price > 0:
        jump = abs(ltp - last_price) / last_price
        if jump > s["max_price_jump_pct"]:
            return f"価格急変（異常値疑い）: {jump:.2%}"
    return None


def _effective_cfg(cfg: Config, sid: str) -> dict:
    """D (Buy & Hold) はベンチマークのため 1 回あたり/保有比率の上限を外す。"""
    if sid != "D_buy_and_hold":
        return cfg
    eff = {**cfg, "risk": {**cfg["risk"]}}
    eff["risk"]["max_order_pct_of_initial"] = Decimal(1)
    eff["risk"]["max_btc_pct_of_equity"] = Decimal(1)
    return eff


def _bootstrap_bars(now: datetime, tf: str, stats: ApiStats) -> list[dict]:
    bars_1m: list[dict] = []
    for back in (2, 1, 0):
        day = (now - timedelta(days=back)).strftime("%Y%m%d")
        bars_1m.extend(api.get_bitbank_1m(day, stats))
    if not bars_1m:
        return []
    out = candles.aggregate_1m(bars_1m, tf)
    for b in out:
        b["source"] = "bootstrap:bitbank-1m"
    return out


def process_tick(root: Path, cfg: Config, *, force: bool = False, no_git: bool = True) -> int:
    tz = cfg["experiment"]["timezone"]
    interval = int(cfg["experiment"]["tick_interval_minutes"])
    store = Store(root)
    stats_api = ApiStats()

    try:
        state = store.load()
    except StoreError as exc:
        log.error("DB(状態)障害のため売買を中止します: %s", exc)
        return 2

    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    tick_id = str(candles.bucket_start(now_ts, interval))

    if state["experiment"] is None:
        with store.transaction() as st:
            init_experiment(st, cfg, now_ts)
        state = store.state

    exp = state["experiment"]

    # --- 冪等性: 同じ 10 分枠の再実行では売買しない ---
    if store.is_tick_processed(tick_id) and not force:
        with store.transaction() as st:
            st["stats"]["skipped_duplicate_ticks"] += 1
            st["stats"]["runs"] += 1
        log.info("tick %s は既に処理済みのためスキップ（二重売買防止）", tick_id)
        dashboard.build(root, store.state, cfg)
        if not no_git:
            store.git_sync(f"skip duplicate tick {tick_id}")
        return 0

    # --- 実験終了判定 ---
    if exp["status"] == "finished":
        log.info("実験は終了済み（%s）。市場データ取得・売買はスキップします。", exp.get("finished_at_jst"))
        return 0
    if now_ts >= int(exp["ends_ts"]):
        with store.transaction() as st:
            st["experiment"]["status"] = "finished"
            st["experiment"]["finished_at_jst"] = _jst(now_ts, tz)
            st["stats"]["runs"] += 1
        report.build(root, store.state, cfg)
        dashboard.build(root, store.state, cfg)
        store.export_csv()
        if not no_git:
            store.git_sync("experiment finished: final report")
        log.info("168 時間が経過したため実験を終了し、最終レポートを出力しました。")
        return 0

    # --- 市場データ取得 ---
    product = exp["product_code"]
    try:
        ticker = api.get_ticker(product, stats_api)
        board = api.get_board(product, stats_api)
        execs = api.get_executions(product, stats_api)
    except ApiError as exc:
        log.error("市場データ取得に失敗したため今回は売買しません: %s", exc)
        with store.transaction() as st:
            st["stats"]["runs"] += 1
            st["stats"]["api_errors"] += stats_api.errors
            st["last_run"] = {"tick_id": tick_id, "at_jst": _jst(now_ts, tz),
                              "status": "api_error", "detail": str(exc)}
        dashboard.build(root, store.state, cfg)
        if not no_git:
            store.git_sync(f"api error at tick {tick_id}")
        return 0

    ticks = state.get("ticks", [])
    last_price = Decimal(ticks[-1]["ltp"]) if ticks else None
    reject = sanity_check(cfg, ticker, last_price)
    if reject:
        log.error("異常値を検出したため今回は売買しません: %s", reject)
        with store.transaction() as st:
            st["stats"]["runs"] += 1
            st["stats"]["data_rejections"] += 1
            st["stats"]["api_errors"] += stats_api.errors
            st["last_run"] = {"tick_id": tick_id, "at_jst": _jst(now_ts, tz),
                              "status": "rejected", "detail": reject}
        dashboard.build(root, store.state, cfg)
        if not no_git:
            store.git_sync(f"rejected tick {tick_id}")
        return 0

    mark = ticker["best_bid"]            # 保守的に売却可能価格で評価
    if exp.get("start_btc_price") in (None, ""):
        exp["start_btc_price"] = str(ticker["ltp"])

    # --- ローソク足の更新（約定履歴ベース + ティック補完） ---
    samples: list[tuple[int, Decimal, Decimal]] = []
    for e in execs:
        try:
            samples.append((_parse_exec_date(e["exec_date"]), e["price"], e["size"]))
        except (ValueError, KeyError):
            continue
    samples = [s for s in samples if s[0] <= now_ts]
    samples.append((now_ts, ticker["ltp"], Decimal(0)))   # 出来高は二重計上しない

    bars = state.setdefault("bars", {})
    max_bars = int(cfg["data"]["max_bars_kept"])
    for tf in candles.TIMEFRAMES:
        current = bars.get(tf, [])
        if not current and cfg["data"]["warmup_bootstrap"]:
            boot = _bootstrap_bars(now, tf, stats_api)
            if boot:
                current = boot
                log.info("%s 足のウォームアップを代理データ(bitbank 1分足)から %d 本生成", tf, len(boot))
        bars[tf] = candles.update_bars(current, samples, tf, max_bars)

    # --- 評価更新とドローダウン ---
    portfolios = {sid: Portfolio.from_dict(d) for sid, d in state["portfolios"].items()}
    for sid in cfg.strategy_ids:
        if sid not in portfolios:         # 設定で後から有効化された戦略
            portfolios[sid] = Portfolio(strategy_id=sid,
                                        initial_jpy=Decimal(exp["initial_capital_jpy"]),
                                        jpy=Decimal(exp["initial_capital_jpy"]))
    dd_stop = cfg["risk"]["max_drawdown_stop"]
    for pf in portfolios.values():
        pf.mark(mark, dd_stop)

    # --- シグナル判定と仮想売買 ---
    new_trades: list[dict] = []
    decisions: dict[str, dict] = {}
    ts_jst = _jst(now_ts, tz)
    for sid in cfg.strategy_ids:
        pf = portfolios[sid]
        fn = strategies.REGISTRY.get(sid)
        if fn is None:
            continue
        sig = fn(cfg, bars, now_ts, pf)
        entry = {"action": sig.action, "reason": sig.reason, "executed": False}
        if sig.action != "HOLD" and pf.halted:
            entry["reason"] = f"halted ({pf.halt_reason}); {sig.reason}"
        elif sig.action != "HOLD" and sig.key and sig.key == pf.last_signal_key:
            entry["reason"] = f"duplicate signal suppressed; {sig.reason}"
        elif sig.action == "BUY":
            notional = Decimal(exp["initial_capital_jpy"]) * sig.order_pct
            tr = pf.buy(ts=ts_jst, notional_jpy=notional, asks=board["asks"], mark_price=mark,
                        cfg=_effective_cfg(cfg, sid), signal_key=sig.key, reason=sig.reason)
            if tr:
                new_trades.append(dict(tr.__dict__))
                entry["executed"] = True
            if sid == "D_buy_and_hold":
                pf.bootstrapped = True
        elif sig.action == "SELL":
            tr = pf.sell(ts=ts_jst, size_btc=pf.btc, bids=board["bids"], mark_price=mark,
                         cfg=_effective_cfg(cfg, sid), signal_key=sig.key, reason=sig.reason)
            if tr:
                new_trades.append(dict(tr.__dict__))
                entry["executed"] = True
        decisions[sid] = entry
        log.info("%s: %s executed=%s (%s)", sid, entry["action"], entry["executed"], entry["reason"])

    # --- 保存（ここまで例外が出ていなければ原子的にコミット） ---
    with store.transaction() as st:
        st["experiment"] = exp
        st["bars"] = bars
        st["portfolios"] = {sid: pf.to_dict() for sid, pf in portfolios.items()}
        st["trades"].extend(new_trades)
        st["ticks"].append({
            "tick_id": tick_id,
            "fetched_at_jst": ts_jst,
            "fetched_at_utc": now.isoformat(timespec="seconds"),
            "exchange_timestamp": ticker["exchange_timestamp"],
            "ltp": str(ticker["ltp"]),
            "best_bid": str(ticker["best_bid"]),
            "best_ask": str(ticker["best_ask"]),
            "best_bid_size": str(ticker["best_bid_size"]),
            "best_ask_size": str(ticker["best_ask_size"]),
            "source": "bitflyer:getticker",
        })
        del st["ticks"][:-int(cfg["data"]["max_ticks_kept"])]
        st["equity_history"].append({
            "ts": now_ts, "jst": ts_jst, "price": str(ticker["ltp"]),
            "equity": {sid: str(pf.equity(mark)) for sid, pf in portfolios.items()},
        })
        del st["equity_history"][:-int(cfg["data"]["max_ticks_kept"])]
        st["stats"]["runs"] += 1
        st["stats"]["api_errors"] += stats_api.errors
        st["stats"]["observed_ticks"] += 1
        expected = (now_ts - int(exp["started_ts"])) // (interval * 60) + 1
        st["stats"]["expected_ticks"] = expected
        st["stats"]["missed_schedules"] = max(0, expected - st["stats"]["observed_ticks"])
        st["last_run"] = {"tick_id": tick_id, "at_jst": ts_jst, "status": "ok",
                          "decisions": decisions, "trades": len(new_trades)}
        store.mark_tick_processed(tick_id)

    store.export_csv()
    dashboard.build(root, store.state, cfg)
    if not no_git:
        store.git_sync(f"tick {tick_id} ({ts_jst}) trades={len(new_trades)}")
    return 0


def loop_ticks(root: Path, cfg: Config, minutes: int, *, force: bool = False,
               no_git: bool = True, sleeper=time.sleep) -> int:
    """1 回の起動で複数ティックを処理する（GitHub の cron 欠落対策）。

    GitHub Actions のスケジュールは遅延・欠落するため、10 分ごとの発火に 1:1 で
    依存すると取りこぼしが増える。1 プロセスを `minutes` 分だけ生かし、10 分の
    境界ごとにティックを処理することで、必要な cron 発火回数を減らす。
    冪等性チェックが効くので、別実行と重なってもティックは二重処理されない。
    """
    interval = int(cfg["experiment"]["tick_interval_minutes"]) * 60
    deadline = time.time() + minutes * 60
    worst = 0
    processed = 0
    while True:
        try:
            rc = process_tick(root, cfg, force=force and processed == 0, no_git=no_git)
        except StoreError as exc:                   # push 失敗など。次のティックは試す
            log.error("ティック処理に失敗: %s", exc)
            rc = 3
        except Exception:                           # noqa: BLE001
            log.exception("ティック処理で予期しないエラー")
            rc = 1
        worst = max(worst, rc)
        processed += 1

        store = Store(root)
        try:
            exp = store.load().get("experiment") or {}
        except StoreError:
            exp = {}
        if exp.get("status") == "finished":
            log.info("実験が終了したのでループを抜けます。")
            break

        next_ts = (int(time.time()) // interval + 1) * interval
        if next_ts >= deadline:
            log.info("ループ終了（%d ティック処理 / 次のティックは次回起動に任せる）", processed)
            break
        wait = max(1, next_ts - int(time.time()) + 2)   # 境界を 2 秒過ぎてから取得
        log.info("次のティックまで %d 秒待機（ループ残り %d 秒）",
                 wait, int(deadline - time.time()))
        sleeper(wait)
    return worst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Bitcoin 仮想売買シミュレーター")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[2]))
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true", help="冪等チェックを無視して実行（検証用）")
    ap.add_argument("--git", action="store_true", help="結果を git commit/push する")
    ap.add_argument("--report-only", action="store_true", help="売買せずレポート/ダッシュボードのみ再生成")
    ap.add_argument("--loop-minutes", type=int, default=0,
                    help="1 回の起動でこの分数だけ常駐し、10 分境界ごとにティックを処理する"
                         "（既定 0 = 1 ティックで終了）")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    root = Path(args.root)
    cfg = Config.load(args.config)

    if args.report_only:
        store = Store(root)
        store.load()
        if store.state.get("experiment") is None:
            log.error("まだ実験が開始されていません。")
            return 1
        report.build(root, store.state, cfg)
        dashboard.build(root, store.state, cfg)
        store.export_csv()
        return 0

    if args.loop_minutes > 0:
        return loop_ticks(root, cfg, args.loop_minutes,
                          force=args.force, no_git=not args.git)

    try:
        return process_tick(root, cfg, force=args.force, no_git=not args.git)
    except GitSyncError as exc:
        log.error("状態はローカルに保存しましたが、リモートへ反映できませんでした: %s", exc)
        log.error("次回の実行で状態を復元できない可能性があるため、ジョブを失敗扱いにします。")
        return 3
    except StoreError as exc:
        log.error("状態保存に失敗（売買は確定していません）: %s", exc)
        return 2
    except Exception:                      # noqa: BLE001 - Actions のログに残す
        log.exception("予期しないエラー。状態は更新されていません。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
