"""FX（証拠金取引）戦略。ロング・ショート両方向。

現物の戦略と 1 対 1 で対応させ、「空売りを解禁すると成績が変わるのか」を
単体で測れるようにしている。

  F_momentum_ls  ↔ 現物 E（短期モメンタム）  : 上昇で買い / 下落で売りから入る
  G_ma_cross_ls  ↔ 現物 A（移動平均クロス）  : ゴールデンクロスで買い / デッドクロスで売り（ドテン）
  H_long_hold_fx ↔ 現物 D（Buy & Hold）     : 開始時にロングを建てて放置（レバレッジ付き）

共通ルール:
  * 同時 1 建玉。方向転換（ドテン）は「決済 → 新規」の 2 取引として扱う。
  * 判定は「いま決済できる価格」（ロング: best_bid / ショート: best_ask）で行う。
  * 指値どおりの約定は仮定しない。約定はその時点の板を歩いた VWAP + スリッページ。
  * 建玉管理料（スワップ）は毎ティック按分して証拠金から引かれる。
  * 証拠金維持率が下限を割ったら、戦略の判断より先に強制ロスカットする。
  * データ不足・ティック欠落時は新規建玉しない。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from .. import candles, indicators, ticks as tickmod
from .account import LONG, SHORT


@dataclass
class FxSignal:
    action: str                  # "OPEN" | "CLOSE" | "REVERSE" | "HOLD"
    key: str
    reason: str
    side: str = ""               # OPEN / REVERSE のときの新しい方向
    notional_jpy: Decimal | None = None
    exit_kind: str = ""


def _hold(reason: str) -> FxSignal:
    return FxSignal(action="HOLD", key="", reason=reason)


def _exit_checks(p: dict, acc, ctx: dict, now_ts: int, params: dict) -> FxSignal | None:
    """利確 / 損切り / 時間切れの共通判定（建玉があるときだけ呼ぶ）。"""
    bid, ask = ctx["best_bid"], ctx["best_ask"]
    change = acc.pnl_pct(bid, ask)
    held = acc.hold_minutes(now_ts) or Decimal(0)
    key = f"{acc.strategy_id}:exit:{p['entry_ts']}"
    if change is not None and params.get("take_profit_pct") and change >= params["take_profit_pct"]:
        return FxSignal("CLOSE", f"{key}:tp", f"take profit {change:+.2%} ({p['side']})",
                        exit_kind="take_profit")
    if change is not None and params.get("stop_loss_pct") and change <= -params["stop_loss_pct"]:
        return FxSignal("CLOSE", f"{key}:sl", f"stop loss {change:+.2%} ({p['side']})",
                        exit_kind="stop_loss")
    if params.get("max_hold_minutes") and held >= int(params["max_hold_minutes"]):
        return FxSignal("CLOSE", f"{key}:timeout", f"max hold {held:.0f}min ({change:+.2%})"
                        if change is not None else f"max hold {held:.0f}min",
                        exit_kind="timeout")
    return None


# --- F: 短期モメンタム（両方向） ------------------------------------------

def strategy_f(cfg: dict, bars: dict[str, Any], now_ts: int, acc, ctx: dict) -> FxSignal:
    p = cfg["strategies"]["F_momentum_ls"]
    series = ctx.get("ticks") or []
    short_min, long_min = int(p["lookback_short_minutes"]), int(p["lookback_long_minutes"])
    mom_s = tickmod.change_pct(series, now_ts, short_min)
    mom_l = tickmod.change_pct(series, now_ts, long_min)

    pos = acc.position
    if pos is not None:
        done = _exit_checks(pos, acc, ctx, now_ts, p)
        if done:
            return done
        # トレンドが逆向きになったらドテン（反対方向へ）
        if mom_s is not None:
            if pos["side"] == LONG and mom_s <= -p["entry_momentum_pct"]:
                return FxSignal("REVERSE", f"F:rev:{pos['entry_ts']}",
                                f"momentum flipped down {mom_s:+.2%}; long -> short",
                                side=SHORT, notional_jpy=p["order_notional_jpy"],
                                exit_kind="reversal")
            if pos["side"] == SHORT and mom_s >= p["entry_momentum_pct"]:
                return FxSignal("REVERSE", f"F:rev:{pos['entry_ts']}",
                                f"momentum flipped up {mom_s:+.2%}; short -> long",
                                side=LONG, notional_jpy=p["order_notional_jpy"],
                                exit_kind="reversal")
        change = acc.pnl_pct(ctx["best_bid"], ctx["best_ask"])
        held = acc.hold_minutes(now_ts) or Decimal(0)
        return _hold(f"holding {pos['side']} {held:.0f}min, pnl {change:+.2%}"
                     if change is not None else f"holding {pos['side']}")

    # --- 新規建玉 ---
    covered = tickmod.covered_minutes(series, now_ts)
    if covered < long_min:
        return _hold(f"warmup: tick history {covered:.0f}min/{long_min}min")
    gap = tickmod.max_gap_minutes(series, now_ts, long_min)
    if gap > int(p["max_data_gap_minutes"]):
        return _hold(f"data gap {gap:.0f}min; no trade")
    cooldown = int(p["cooldown_minutes"])
    if acc.last_exit_ts and (now_ts - int(acc.last_exit_ts)) / 60 < cooldown:
        left = cooldown - (now_ts - int(acc.last_exit_ts)) / 60
        return _hold(f"cooldown {left:.0f}min left")
    if mom_s is None or mom_l is None:
        return _hold("momentum unavailable")
    spread = ctx.get("spread_pct")
    if spread is not None and spread > p["entry_max_spread_pct"]:
        return _hold(f"spread {spread:.3%} too wide")

    bucket = candles.bucket_start(now_ts, 10)
    if mom_s >= p["entry_momentum_pct"] and mom_l > 0:
        return FxSignal("OPEN", f"F:entry:{bucket}:L",
                        f"momentum {short_min}min {mom_s:+.2%}, {long_min}min {mom_l:+.2%}",
                        side=LONG, notional_jpy=p["order_notional_jpy"])
    if mom_s <= -p["entry_momentum_pct"] and mom_l < 0:
        return FxSignal("OPEN", f"F:entry:{bucket}:S",
                        f"momentum {short_min}min {mom_s:+.2%}, {long_min}min {mom_l:+.2%}",
                        side=SHORT, notional_jpy=p["order_notional_jpy"])
    return _hold(f"{short_min}min {mom_s:+.2%} / {long_min}min {mom_l:+.2%}: no signal")


# --- G: 移動平均クロス（両方向・ドテン） ----------------------------------

def strategy_g(cfg: dict, bars: dict[str, Any], now_ts: int, acc, ctx: dict) -> FxSignal:
    p = cfg["strategies"]["G_ma_cross_ls"]
    tf = p["timeframe"]
    cb = candles.closed_bars(bars.get(tf, []), now_ts, tf)
    cl = candles.closes(cb)
    need = int(p["long_window"]) + 1

    pos = acc.position
    if pos is not None:
        done = _exit_checks(pos, acc, ctx, now_ts, p)
        if done:
            return done

    if len(cl) < need:
        return _hold(f"warmup: {len(cl)}/{need} closed {tf} bars")
    s_now = indicators.sma(cl, int(p["short_window"]))
    l_now = indicators.sma(cl, int(p["long_window"]))
    s_prev = indicators.sma(cl[:-1], int(p["short_window"]))
    l_prev = indicators.sma(cl[:-1], int(p["long_window"]))
    bar_ts = cb[-1]["ts"]
    golden = s_prev <= l_prev and s_now > l_now
    dead = s_prev >= l_prev and s_now < l_now

    if pos is not None:
        if golden and pos["side"] == SHORT:
            return FxSignal("REVERSE", f"G:{tf}:{bar_ts}:L",
                            f"golden cross (short={s_now:.0f} long={l_now:.0f}); short -> long",
                            side=LONG, notional_jpy=p["order_notional_jpy"], exit_kind="reversal")
        if dead and pos["side"] == LONG:
            return FxSignal("REVERSE", f"G:{tf}:{bar_ts}:S",
                            f"dead cross (short={s_now:.0f} long={l_now:.0f}); long -> short",
                            side=SHORT, notional_jpy=p["order_notional_jpy"], exit_kind="reversal")
        change = acc.pnl_pct(ctx["best_bid"], ctx["best_ask"])
        return _hold(f"holding {pos['side']}, pnl {change:+.2%}" if change is not None
                     else f"holding {pos['side']}")

    cooldown = int(p["cooldown_minutes"])
    if acc.last_exit_ts and (now_ts - int(acc.last_exit_ts)) / 60 < cooldown:
        return _hold("cooldown after exit")
    if golden:
        return FxSignal("OPEN", f"G:{tf}:{bar_ts}:L",
                        f"golden cross short={s_now:.0f} long={l_now:.0f}",
                        side=LONG, notional_jpy=p["order_notional_jpy"])
    if dead:
        return FxSignal("OPEN", f"G:{tf}:{bar_ts}:S",
                        f"dead cross short={s_now:.0f} long={l_now:.0f}",
                        side=SHORT, notional_jpy=p["order_notional_jpy"])
    return _hold(f"no cross (short={s_now:.0f}, long={l_now:.0f})")


# --- H: レバレッジ・ロングホールド（ベンチマーク） ------------------------

def strategy_h(cfg: dict, bars: dict[str, Any], now_ts: int, acc, ctx: dict) -> FxSignal:
    """開始時に 1 度だけロングを建てて放置する比較用（現物 D の FX 版）。

    利確も損切りもしないが、**証拠金維持率を割れば強制ロスカットされる**。
    これがレバレッジのリスクそのものなので、あえて止めない。
    """
    p = cfg["strategies"]["H_long_hold_fx"]
    if acc.position is not None:
        return _hold("long & hold: holding (no further trades)")
    if acc.opens > 0:
        return _hold("long & hold: position already closed (liquidated?)")
    notional = acc.collateral_jpy * p["leverage_target"]
    return FxSignal("OPEN", "H:initial", "initial leveraged long (benchmark)",
                    side=LONG, notional_jpy=notional)


REGISTRY = {
    "F_momentum_ls": strategy_f,
    "G_ma_cross_ls": strategy_g,
    "H_long_hold_fx": strategy_h,
}
