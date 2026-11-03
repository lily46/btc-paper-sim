"""FX（証拠金取引）実験の 1 ティック処理。

現物版 `btcsim.run` と同じ骨格だが、次が違う:
  * 対象は FX_BTC_JPY（ショート可・レバレッジあり）
  * 建玉管理料（スワップ）を毎ティック按分して引く
  * **戦略の判断より先に強制ロスカットを判定する**
  * ドテン（REVERSE）は「決済 → 新規」の 2 取引として処理する
  * 状態は state_fx/、公開物は docs/fx-*.{json,html}、reports/fx_final_report.*

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

from .. import api, candles
from ..api import ApiError, ApiStats
from ..config import Config, FX_CONFIG_PATH
from ..run import _bootstrap_bars, _jst, sanity_check
from ..store import GitSyncError, Store, StoreError
from . import dashboard as fx_dashboard
from . import report as fx_report
from . import strategies as fx_strategies
from .account import MarginAccount

log = logging.getLogger("btcsim.fx")

STATE_DIR = "state_fx"
GIT_PATHS = [STATE_DIR, "docs", "reports"]


def init_experiment(state: dict, cfg: Config, now_ts: int) -> None:
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
        "leverage": str(cfg["risk"]["leverage"]),
        "market": "margin",            # 現物実験と区別するための印
    }
    state["portfolios"] = {
        sid: MarginAccount(strategy_id=sid, initial_jpy=exp["initial_capital_jpy"],
                           collateral_jpy=exp["initial_capital_jpy"]).to_dict()
        for sid in cfg.strategy_ids
    }
    log.info("FX 実験を初期化: 開始 %s / 終了予定 %s / レバレッジ %s倍 / 戦略 %s",
             state["experiment"]["started_at_jst"], state["experiment"]["ends_at_jst"],
             cfg["risk"]["leverage"], ", ".join(state["portfolios"]))


def process_tick(root: Path, cfg: Config, *, force: bool = False, no_git: bool = True) -> int:
    tz = cfg["experiment"]["timezone"]
    interval = int(cfg["experiment"]["tick_interval_minutes"])
    store = Store(root, STATE_DIR)
    stats_api = ApiStats()

    try:
        state = store.load()
    except StoreError as exc:
        log.error("状態の読み込みに失敗したため売買を中止します: %s", exc)
        return 2

    now = datetime.now(timezone.utc)
    now_ts = int(now.timestamp())
    tick_id = str(candles.bucket_start(now_ts, interval))

    if state["experiment"] is None:
        with store.transaction() as st:
            init_experiment(st, cfg, now_ts)
        state = store.state
    exp = state["experiment"]

    if store.is_tick_processed(tick_id) and not force:
        with store.transaction() as st:
            st["stats"]["skipped_duplicate_ticks"] += 1
            st["stats"]["runs"] += 1
        log.info("tick %s は処理済みのためスキップ（二重発注防止）", tick_id)
        fx_dashboard.build(root, store.state, cfg)
        if not no_git:
            store.git_sync(f"[fx] skip duplicate tick {tick_id}", GIT_PATHS)
        return 0

    if exp["status"] == "finished":
        log.info("FX 実験は終了済み（%s）。市場データ取得・売買はスキップします。",
                 exp.get("finished_at_jst"))
        return 0

    if now_ts >= int(exp["ends_ts"]):
        with store.transaction() as st:
            st["experiment"]["status"] = "finished"
            st["experiment"]["finished_at_jst"] = _jst(now_ts, tz)
            st["stats"]["runs"] += 1
        fx_report.build(root, store.state, cfg)
        fx_dashboard.build(root, store.state, cfg)
        store.export_csv()
        if not no_git:
            store.git_sync("[fx] experiment finished: final report", GIT_PATHS)
        log.info("168 時間が経過したため FX 実験を終了し、最終レポートを出力しました。")
        return 0

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
        fx_dashboard.build(root, store.state, cfg)
        if not no_git:
            store.git_sync(f"[fx] api error at tick {tick_id}", GIT_PATHS)
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
        fx_dashboard.build(root, store.state, cfg)
        if not no_git:
            store.git_sync(f"[fx] rejected tick {tick_id}", GIT_PATHS)
        return 0

    bid, ask = ticker["best_bid"], ticker["best_ask"]
    spread_pct = (ask - bid) / bid
    if exp.get("start_btc_price") in (None, ""):
        exp["start_btc_price"] = str(ticker["ltp"])

    ts_jst = _jst(now_ts, tz)
    tick_record = {
        "tick_id": tick_id, "ts": now_ts, "fetched_at_jst": ts_jst,
        "fetched_at_utc": now.isoformat(timespec="seconds"),
        "exchange_timestamp": ticker["exchange_timestamp"],
        "ltp": str(ticker["ltp"]), "best_bid": str(bid), "best_ask": str(ask),
        "best_bid_size": str(ticker["best_bid_size"]),
        "best_ask_size": str(ticker["best_ask_size"]),
        "source": f"bitflyer:getticker:{product}",
    }

    # --- ローソク足（G 用） ---
    samples: list[tuple[int, Decimal, Decimal]] = []
    from ..run import _parse_exec_date
    for e in execs:
        try:
            samples.append((_parse_exec_date(e["exec_date"]), e["price"], e["size"]))
        except (ValueError, KeyError):
            continue
    samples = [s for s in samples if s[0] <= now_ts]
    samples.append((now_ts, ticker["ltp"], Decimal(0)))
    bars = state.setdefault("bars", {})
    max_bars = int(cfg["data"]["max_bars_kept"])
    for tf in candles.TIMEFRAMES:
        current = bars.get(tf, [])
        if not current and cfg["data"]["warmup_bootstrap"]:
            boot = _bootstrap_bars(now, tf, stats_api)
            if boot:
                current = boot
                log.info("%s 足のウォームアップを代理データから %d 本生成", tf, len(boot))
        bars[tf] = candles.update_bars(current, samples, tf, max_bars)

    accounts = {sid: MarginAccount.from_dict(d) for sid, d in state["portfolios"].items()}
    for sid in cfg.strategy_ids:
        if sid not in accounts:
            accounts[sid] = MarginAccount(
                strategy_id=sid, initial_jpy=Decimal(exp["initial_capital_jpy"]),
                collateral_jpy=Decimal(exp["initial_capital_jpy"]))

    dd_stop = cfg["risk"]["max_drawdown_stop"]
    swap_rate = cfg["fees"]["swap_rate_per_day"]
    ctx = {"ticks": state.get("ticks", []) + [tick_record],
           "best_bid": bid, "best_ask": ask, "spread_pct": spread_pct, "now_ts": now_ts}

    new_trades: list[dict] = []
    decisions: dict[str, dict] = {}
    for sid in cfg.strategy_ids:
        acc = accounts[sid]
        fn = fx_strategies.REGISTRY.get(sid)
        if fn is None:
            continue

        # 1) 建玉管理料を先に反映（保有しているだけでコストがかかる）
        swap = acc.accrue_swap(now_ts, bid, ask, swap_rate)
        acc.mark(bid, ask, dd_stop)

        # 2) 強制ロスカット判定は戦略の判断より先
        if acc.needs_liquidation(bid, ask, cfg):
            ratio = acc.margin_ratio(bid, ask, cfg["risk"]["leverage"])
            tr = acc.close_position(
                ts=ts_jst, board=board, bid=bid, ask=ask, cfg=cfg,
                signal_key=f"{sid}:liquidation:{now_ts}",
                reason=f"margin call: ratio {ratio:.2%} < {cfg['risk']['liquidation_margin_ratio']:.0%}"
                if ratio is not None else "margin call",
                now_ts=now_ts, exit_kind="liquidation", spread_pct=spread_pct)
            if tr:
                new_trades.append(dict(tr.__dict__))
                log.warning("%s: 強制ロスカット（維持率 %s）", sid,
                            f"{ratio:.2%}" if ratio is not None else "?")
            decisions[sid] = {"action": "LIQUIDATION", "reason": tr.reason if tr else "margin call",
                              "executed": bool(tr), "swap_jpy": str(swap)}
            acc.mark(bid, ask, dd_stop)
            continue

        sig = fn(cfg, bars, now_ts, acc, ctx)
        entry = {"action": sig.action, "reason": sig.reason, "executed": False,
                 "side": sig.side, "swap_jpy": str(swap)}

        opens_blocked = acc.halted
        if sig.action in ("CLOSE", "REVERSE") and acc.position is not None:
            tr = acc.close_position(ts=ts_jst, board=board, bid=bid, ask=ask, cfg=cfg,
                                    signal_key=sig.key, reason=sig.reason, now_ts=now_ts,
                                    exit_kind=sig.exit_kind or "signal", spread_pct=spread_pct)
            if tr:
                new_trades.append(dict(tr.__dict__))
                entry["executed"] = True
            # ドテン: 決済できたら反対方向を新規建玉（停止中は建てない）
            if sig.action == "REVERSE" and tr and not opens_blocked:
                tr2 = acc.open_position(
                    ts=ts_jst, side=sig.side, notional_jpy=sig.notional_jpy or Decimal(0),
                    board=board, bid=bid, ask=ask, cfg=cfg, signal_key=sig.key + ":open",
                    reason=sig.reason, now_ts=now_ts, spread_pct=spread_pct)
                if tr2:
                    new_trades.append(dict(tr2.__dict__))
        elif sig.action == "OPEN":
            if opens_blocked:
                entry["reason"] = f"halted ({acc.halt_reason}); {sig.reason}"
            elif sig.key and sig.key == acc.last_signal_key:
                entry["reason"] = f"duplicate signal suppressed; {sig.reason}"
            else:
                tr = acc.open_position(
                    ts=ts_jst, side=sig.side, notional_jpy=sig.notional_jpy or Decimal(0),
                    board=board, bid=bid, ask=ask, cfg=cfg, signal_key=sig.key,
                    reason=sig.reason, now_ts=now_ts, spread_pct=spread_pct)
                if tr:
                    new_trades.append(dict(tr.__dict__))
                    entry["executed"] = True

        acc.mark(bid, ask, dd_stop)
        decisions[sid] = entry
        log.info("%s: %s%s executed=%s (%s)", sid, sig.action,
                 f" {sig.side}" if sig.side else "", entry["executed"], entry["reason"])

    with store.transaction() as st:
        st["experiment"] = exp
        st["bars"] = bars
        st["portfolios"] = {sid: acc.to_dict() for sid, acc in accounts.items()}
        st["trades"].extend(new_trades)
        st["ticks"].append(tick_record)
        del st["ticks"][:-int(cfg["data"]["max_ticks_kept"])]
        st["equity_history"].append({
            "ts": now_ts, "jst": ts_jst, "price": str(ticker["ltp"]),
            "equity": {sid: str(acc.equity(bid, ask)) for sid, acc in accounts.items()},
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
    fx_dashboard.build(root, store.state, cfg)
    if not no_git:
        store.git_sync(f"[fx] tick {tick_id} ({ts_jst}) trades={len(new_trades)}", GIT_PATHS)
    return 0


def loop_ticks(root: Path, cfg: Config, minutes: int, *, force: bool = False,
               no_git: bool = True, sleeper=time.sleep) -> int:
    """現物版と同じ「1 起動で複数ティック」方式（cron 欠落対策）。"""
    interval = int(cfg["experiment"]["tick_interval_minutes"]) * 60
    deadline = time.time() + minutes * 60
    worst = 0
    processed = 0
    max_iters = minutes * 60 // interval + 2
    while processed < max_iters:
        try:
            rc = process_tick(root, cfg, force=force and processed == 0, no_git=no_git)
        except StoreError as exc:
            log.error("ティック処理に失敗: %s", exc)
            rc = 3
        except Exception:                       # noqa: BLE001
            log.exception("ティック処理で予期しないエラー")
            rc = 1
        worst = max(worst, rc)
        processed += 1

        store = Store(root, STATE_DIR)
        try:
            exp = store.load().get("experiment") or {}
        except StoreError:
            exp = {}
        if exp.get("status") == "finished":
            log.info("FX 実験が終了したのでループを抜けます。")
            break
        next_ts = (int(time.time()) // interval + 1) * interval
        if next_ts >= deadline:
            log.info("ループ終了（%d ティック処理）", processed)
            break
        wait = max(1, next_ts - int(time.time()) + 2)
        log.info("次のティックまで %d 秒待機（ループ残り %d 秒）",
                 wait, int(deadline - time.time()))
        sleeper(wait)
    return worst


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BTC FX（証拠金取引）仮想売買シミュレーター")
    ap.add_argument("--root", default=str(Path(__file__).resolve().parents[3]))
    ap.add_argument("--config", default=None)
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--git", action="store_true")
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--loop-minutes", type=int, default=0)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    root = Path(args.root)
    cfg = Config.load(args.config or FX_CONFIG_PATH)

    if args.report_only:
        store = Store(root, STATE_DIR)
        store.load()
        if store.state.get("experiment") is None:
            log.error("FX 実験はまだ開始されていません。")
            return 1
        fx_report.build(root, store.state, cfg)
        fx_dashboard.build(root, store.state, cfg)
        store.export_csv()
        return 0

    if args.loop_minutes > 0:
        return loop_ticks(root, cfg, args.loop_minutes, force=args.force, no_git=not args.git)
    try:
        return process_tick(root, cfg, force=args.force, no_git=not args.git)
    except GitSyncError as exc:
        log.error("状態は保存しましたがリモートへ反映できませんでした: %s", exc)
        return 3
    except StoreError as exc:
        log.error("状態保存に失敗（売買は確定していません）: %s", exc)
        return 2
    except Exception:                           # noqa: BLE001
        log.exception("予期しないエラー。状態は更新されていません。")
        return 1


if __name__ == "__main__":
    sys.exit(main())
