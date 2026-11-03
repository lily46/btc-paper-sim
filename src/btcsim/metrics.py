"""集計ロジック（ダッシュボードと最終レポートで共通利用）。

勝率の定義:
  「売却（決済）1 回を 1 トレードとみなし、その実現損益（手数料込み）が
   プラスだったものの割合」= wins / (wins + losses)。
  未決済のポジションと、実現損益がちょうど 0 の決済は分母に含めない。
  売却が 0 回の戦略（例: Buy & Hold）は勝率を None とする。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from .portfolio import Portfolio, q_jpy


def _pct(v: Decimal) -> str:
    return f"{v * 100:.4f}"


def summarize(state: dict[str, Any], cfg: dict) -> dict[str, Any]:
    exp = state.get("experiment") or {}
    ticks = state.get("ticks", [])
    last_tick = ticks[-1] if ticks else None
    mark = Decimal(last_tick["best_bid"]) if last_tick else Decimal(0)
    ltp = Decimal(last_tick["ltp"]) if last_tick else Decimal(0)
    start_price = Decimal(exp.get("start_btc_price") or 0)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    ends_ts = int(exp.get("ends_ts") or 0)

    windows = state.get("strategy_windows", {})
    trades = state.get("trades", [])
    bh = None
    rows: dict[str, Any] = {}
    for sid, d in state.get("portfolios", {}).items():
        pf = Portfolio.from_dict(d)
        equity = pf.equity(mark)
        pnl = equity - pf.initial_jpy
        closed = pf.wins + pf.losses
        win = windows.get(sid) or {}
        win_ends = int(win["ends_ts"]) if win.get("ends_ts") else ends_ts
        avg_hold = pf.avg_hold_minutes()
        rows[sid] = {
            "strategy": sid,
            "equity_jpy": str(equity),
            "jpy_balance": str(q_jpy(pf.jpy)),
            "btc_balance": str(pf.btc),
            "btc_value_jpy": str(q_jpy(pf.btc * mark)),
            "avg_cost_jpy": str(pf.avg_cost()),
            "unrealized_pnl_jpy": str(pf.unrealized_pnl(mark)),
            "realized_pnl_jpy": str(q_jpy(pf.realized_pnl)),
            "total_pnl_jpy": str(q_jpy(pnl)),
            "total_pnl_pct": _pct(pnl / pf.initial_jpy) if pf.initial_jpy else "0",
            "trades": pf.trades,
            "buys": pf.buys,
            "sells": pf.sells,
            "fees_jpy": str(q_jpy(pf.fees_paid)),
            "max_drawdown_pct": _pct(pf.max_drawdown),
            "win_rate_pct": _pct(Decimal(pf.wins) / Decimal(closed)) if closed else None,
            "closed_trades": closed,
            "halted": pf.halted,
            "halt_reason": pf.halt_reason,
            # --- 戦略ごとの実験期間（E は A〜D と開始日が違う） ---
            "started_at_jst": win.get("started_at_jst") or exp.get("started_at_jst"),
            "ends_at_jst": win.get("ends_at_jst") or exp.get("ends_at_jst"),
            "window_status": win.get("status") or exp.get("status"),
            "window_finished_at_jst": win.get("finished_at_jst"),
            "remaining_seconds": max(0, win_ends - now_ts),
            "remaining_human": _human(max(0, win_ends - now_ts)),
            "own_window": not win.get("inherited", True),
            # --- 短期売買の分析用 ---
            "avg_hold_minutes": f"{avg_hold:.1f}" if avg_hold is not None else None,
            "open_position": pf.btc > 0,
            "position_entry_price": pf.position_entry_price,
            "last_reason": ((state.get("last_run") or {}).get("decisions") or {}).get(sid, {}).get("reason"),
            **_trade_analysis(sid, trades, pf),
        }
        if sid == "D_buy_and_hold":
            bh = equity

    if bh is not None:
        for row in rows.values():
            row["vs_buy_and_hold_jpy"] = str(q_jpy(Decimal(row["equity_jpy"]) - bh))

    stats = state.get("stats", {})
    common = _common_period(state, rows, now_ts)
    return {
        "common_period": common,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "paper_trading_only": True,
        "experiment": {
            "started_at_jst": exp.get("started_at_jst"),
            "ends_at_jst": exp.get("ends_at_jst"),
            "status": exp.get("status"),
            "finished_at_jst": exp.get("finished_at_jst"),
            "timezone": exp.get("timezone"),
            "product_code": exp.get("product_code"),
            "initial_capital_jpy": exp.get("initial_capital_jpy"),
            "remaining_seconds": max(0, ends_ts - now_ts) if ends_ts else None,
            "remaining_human": _human(max(0, ends_ts - now_ts)) if ends_ts else None,
        },
        "market": {
            "last_fetch_jst": last_tick["fetched_at_jst"] if last_tick else None,
            "ltp": str(ltp),
            "best_bid": str(mark),
            "best_ask": last_tick["best_ask"] if last_tick else None,
            "start_price": str(start_price),
            "price_change_pct": _pct((ltp - start_price) / start_price) if start_price else None,
            "source": "bitFlyer Public API (/v1/getticker, /v1/getboard, /v1/getexecutions)",
        },
        "strategies": rows,
        "strategy_windows": windows,
        "stats": stats,
        "last_run": state.get("last_run"),
    }


def _human(seconds: int) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}日 {h}時間 {m}分"


def _trade_analysis(sid: str, trades: list[dict[str, Any]], pf: Portfolio) -> dict[str, Any]:
    """決済トレードの内訳（短期売買の評価用）。

    - エグジット理由の内訳（利確 / 損切り / 時間切れ / トレンド反転）
    - 平均利益・平均損失と損益分岐勝率
      損益分岐勝率 = 平均損失 / (平均利益 + 平均損失)
      （手数料込みの実現損益で計算するので、手数料負けも織り込まれる）
    """
    sells = [t for t in trades if t.get("strategy") == sid and t.get("side") == "SELL"]
    kinds: dict[str, int] = {}
    wins: list[Decimal] = []
    losses: list[Decimal] = []
    for t in sells:
        kinds[t.get("exit_kind") or "signal"] = kinds.get(t.get("exit_kind") or "signal", 0) + 1
        pnl = Decimal(str(t.get("realized_pnl") or 0))
        if pnl > 0:
            wins.append(pnl)
        elif pnl < 0:
            losses.append(-pnl)
    avg_win = sum(wins, Decimal(0)) / Decimal(len(wins)) if wins else None
    avg_loss = sum(losses, Decimal(0)) / Decimal(len(losses)) if losses else None
    breakeven = None
    if avg_win is not None and avg_loss is not None and (avg_win + avg_loss) > 0:
        breakeven = _pct(avg_loss / (avg_win + avg_loss))
    return {
        "exit_breakdown": kinds,
        "avg_win_jpy": str(q_jpy(avg_win)) if avg_win is not None else None,
        "avg_loss_jpy": str(q_jpy(avg_loss)) if avg_loss is not None else None,
        "breakeven_win_rate_pct": breakeven,
    }


def _common_period(state: dict[str, Any], rows: dict[str, Any], now_ts: int) -> dict[str, Any]:
    """全戦略が走っている共通期間での比較。

    戦略 E は A〜D より後から始まるため、全期間の最終損益だけでは公平に
    比較できない。「一番遅く始まった戦略の開始時刻」以降の資産変化を並べる。
    """
    windows = state.get("strategy_windows", {})
    hist = state.get("equity_history", [])
    if not windows or not hist:
        return {"available": False}
    start = max(int(w["started_ts"]) for w in windows.values() if w.get("started_ts"))
    base = next((h for h in hist if int(h["ts"]) >= start), None)
    latest = hist[-1]
    if base is None:
        return {"available": False}
    out: dict[str, Any] = {}
    for sid in rows:
        b = base["equity"].get(sid)
        c = latest["equity"].get(sid)
        if b is None or c is None:
            continue
        b, c = Decimal(str(b)), Decimal(str(c))
        out[sid] = {
            "start_equity_jpy": str(q_jpy(b)),
            "end_equity_jpy": str(q_jpy(c)),
            "pnl_jpy": str(q_jpy(c - b)),
            "pnl_pct": _pct((c - b) / b) if b > 0 else None,
        }
    bh = out.get("D_buy_and_hold")
    if bh:
        for sid, row in out.items():
            row["vs_buy_and_hold_jpy"] = str(q_jpy(Decimal(row["pnl_jpy"]) - Decimal(bh["pnl_jpy"])))
    base_price = base.get("price")
    return {
        "available": True,
        "from_jst": base.get("jst"),
        "to_jst": latest.get("jst"),
        "hours": round((int(latest["ts"]) - int(base["ts"])) / 3600, 2),
        "btc_change_pct": (_pct((Decimal(str(latest["price"])) - Decimal(str(base_price)))
                                / Decimal(str(base_price))) if base_price else None),
        "strategies": out,
    }
