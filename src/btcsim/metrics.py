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

    bh = None
    rows: dict[str, Any] = {}
    for sid, d in state.get("portfolios", {}).items():
        pf = Portfolio.from_dict(d)
        equity = pf.equity(mark)
        pnl = equity - pf.initial_jpy
        closed = pf.wins + pf.losses
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
        }
        if sid == "D_buy_and_hold":
            bh = equity

    if bh is not None:
        for row in rows.values():
            row["vs_buy_and_hold_jpy"] = str(q_jpy(Decimal(row["equity_jpy"]) - bh))

    stats = state.get("stats", {})
    return {
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
        "stats": stats,
        "last_run": state.get("last_run"),
    }


def _human(seconds: int) -> str:
    d, rem = divmod(int(seconds), 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    return f"{d}日 {h}時間 {m}分"
