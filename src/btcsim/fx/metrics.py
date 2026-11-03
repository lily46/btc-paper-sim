"""FX 実験の集計（ダッシュボードと最終レポートで共通）。

勝率の定義は現物と同じ「決済 1 回を 1 トレードとし、手数料込みの実現損益が
プラスだった割合」。ただし FX では**強制ロスカットも決済 1 回**として数える。
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ..metrics import _human, _pct
from ..portfolio import q_jpy
from .account import LONG, SHORT, MarginAccount


def summarize(state: dict[str, Any], cfg: dict) -> dict[str, Any]:
    exp = state.get("experiment") or {}
    ticks = state.get("ticks", [])
    last = ticks[-1] if ticks else None
    bid = Decimal(last["best_bid"]) if last else Decimal(0)
    ask = Decimal(last["best_ask"]) if last else Decimal(0)
    ltp = Decimal(last["ltp"]) if last else Decimal(0)
    start_price = Decimal(exp.get("start_btc_price") or 0)
    now_ts = int(datetime.now(timezone.utc).timestamp())
    ends_ts = int(exp.get("ends_ts") or 0)
    leverage = cfg["risk"]["leverage"]
    trades = state.get("trades", [])

    rows: dict[str, Any] = {}
    for sid, d in state.get("portfolios", {}).items():
        acc = MarginAccount.from_dict(d)
        equity = acc.equity(bid, ask)
        pnl = equity - acc.initial_jpy
        closed = acc.wins + acc.losses
        pos = acc.pos()
        ratio = acc.margin_ratio(bid, ask, leverage)
        avg_hold = acc.avg_hold_minutes()
        rows[sid] = {
            "strategy": sid,
            "equity_jpy": str(equity),
            "collateral_jpy": str(q_jpy(acc.collateral_jpy)),
            "unrealized_pnl_jpy": str(acc.unrealized(bid, ask)),
            "realized_pnl_jpy": str(q_jpy(acc.realized_pnl)),
            "total_pnl_jpy": str(q_jpy(pnl)),
            "total_pnl_pct": _pct(pnl / acc.initial_jpy) if acc.initial_jpy else "0",
            "position_side": pos.side if pos else None,
            "position_size_btc": pos.size if pos else "0",
            "position_entry_price": pos.entry_price if pos else None,
            "position_notional_jpy": str(acc.notional(bid, ask)),
            "position_pnl_pct": _pct(acc.pnl_pct(bid, ask)) if pos else None,
            "margin_ratio_pct": _pct(ratio) if ratio is not None else None,
            "effective_leverage": (str(q_jpy(acc.notional(bid, ask) / equity))
                                   if pos and equity > 0 else "0"),
            "hold_minutes": (f"{acc.hold_minutes(now_ts):.1f}" if pos else None),
            "trades": acc.trades,
            "opens": acc.opens,
            "closes": acc.closes,
            "longs": acc.longs,
            "shorts": acc.shorts,
            "liquidations": acc.liquidations,
            "closed_trades": closed,
            "win_rate_pct": _pct(Decimal(acc.wins) / Decimal(closed)) if closed else None,
            "fees_jpy": str(q_jpy(acc.fees_paid)),
            "swap_jpy": str(q_jpy(acc.swap_paid)),
            "total_cost_jpy": str(q_jpy(acc.fees_paid + acc.swap_paid)),
            "max_drawdown_pct": _pct(acc.max_drawdown),
            "avg_hold_minutes": f"{avg_hold:.1f}" if avg_hold is not None else None,
            "halted": acc.halted,
            "halt_reason": acc.halt_reason,
            "last_reason": ((state.get("last_run") or {}).get("decisions") or {}).get(sid, {}).get("reason"),
            **_trade_analysis(sid, trades),
        }

    stats = state.get("stats", {})
    return {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "paper_trading_only": True,
        "market": "margin",
        "experiment": {
            "started_at_jst": exp.get("started_at_jst"),
            "ends_at_jst": exp.get("ends_at_jst"),
            "status": exp.get("status"),
            "finished_at_jst": exp.get("finished_at_jst"),
            "timezone": exp.get("timezone"),
            "product_code": exp.get("product_code"),
            "initial_capital_jpy": exp.get("initial_capital_jpy"),
            "leverage": str(leverage),
            "liquidation_margin_ratio_pct": _pct(cfg["risk"]["liquidation_margin_ratio"]),
            "swap_rate_per_day_pct": _pct(cfg["fees"]["swap_rate_per_day"]),
            "remaining_seconds": max(0, ends_ts - now_ts) if ends_ts else None,
            "remaining_human": _human(max(0, ends_ts - now_ts)) if ends_ts else None,
        },
        "market_data": {
            "last_fetch_jst": last["fetched_at_jst"] if last else None,
            "ltp": str(ltp),
            "best_bid": str(bid),
            "best_ask": str(ask),
            "start_price": str(start_price),
            "price_change_pct": _pct((ltp - start_price) / start_price) if start_price else None,
            "source": "bitFlyer Public API (FX_BTC_JPY: /v1/getticker, /v1/getboard, /v1/getexecutions)",
        },
        "strategies": rows,
        "stats": stats,
        "last_run": state.get("last_run"),
    }


def _trade_analysis(sid: str, trades: list[dict[str, Any]]) -> dict[str, Any]:
    closes = [t for t in trades if t.get("strategy") == sid and t.get("action") == "CLOSE"]
    kinds: dict[str, int] = {}
    by_side: dict[str, dict[str, Any]] = {}
    wins: list[Decimal] = []
    losses: list[Decimal] = []
    for t in closes:
        kinds[t.get("exit_kind") or "signal"] = kinds.get(t.get("exit_kind") or "signal", 0) + 1
        pnl = Decimal(str(t.get("realized_pnl") or 0))
        side = t.get("side") or "?"
        b = by_side.setdefault(side, {"closes": 0, "wins": 0, "pnl": Decimal(0)})
        b["closes"] += 1
        b["pnl"] += pnl
        if pnl > 0:
            wins.append(pnl)
            b["wins"] += 1
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
        "by_side": {
            side: {
                "closes": v["closes"],
                "win_rate_pct": _pct(Decimal(v["wins"]) / Decimal(v["closes"])) if v["closes"] else None,
                "pnl_jpy": str(q_jpy(v["pnl"])),
            } for side, v in by_side.items()
        },
    }


def spot_comparison(fx_summary: dict[str, Any], spot_state: dict[str, Any] | None,
                    spot_cfg: dict | None) -> dict[str, Any]:
    """現物実験（state/）との比較。対応する戦略ペアを並べる。

    FX は後から始めたので、共通期間（FX の開始以降）の損益で比べる。
    """
    pairs = {"F_momentum_ls": "E_short_momentum",
             "G_ma_cross_ls": "A_ma_cross",
             "H_long_hold_fx": "D_buy_and_hold"}
    if not spot_state or not spot_cfg:
        return {"available": False, "pairs": pairs}
    from ..metrics import summarize as spot_summarize
    spot = spot_summarize(spot_state, spot_cfg)
    hist = spot_state.get("equity_history") or []
    fx_start = fx_summary.get("experiment", {}).get("started_at_jst")
    base = next((h for h in hist if h.get("jst", "") >= (fx_start or "")), None)
    if base is None:
        # FX 開始より後の現物スナップショットがまだ無い（開始直後）
        return {"available": False, "reason": "現物側に FX 開始後のデータがまだありません",
                "pairs": pairs}
    out: dict[str, Any] = {}
    for fx_sid, spot_sid in pairs.items():
        fx_row = fx_summary["strategies"].get(fx_sid)
        spot_row = spot["strategies"].get(spot_sid)
        if not fx_row or not spot_row:
            continue
        entry = {
            "fx_strategy": fx_sid, "spot_strategy": spot_sid,
            "fx_pnl_jpy": fx_row["total_pnl_jpy"],
            "fx_trades": fx_row["trades"], "fx_shorts": fx_row["shorts"],
            "spot_pnl_jpy": spot_row["total_pnl_jpy"],
            "spot_trades": spot_row["trades"],
        }
        if base and hist:
            b = base["equity"].get(spot_sid)
            c = hist[-1]["equity"].get(spot_sid)
            if b and c:
                entry["spot_pnl_since_fx_start_jpy"] = str(q_jpy(Decimal(str(c)) - Decimal(str(b))))
                entry["short_effect_jpy"] = str(q_jpy(
                    Decimal(fx_row["total_pnl_jpy"]) - (Decimal(str(c)) - Decimal(str(b)))))
        out[fx_sid] = entry
    return {"available": bool(out), "since_jst": base.get("jst") if base else None,
            "pairs": out}
