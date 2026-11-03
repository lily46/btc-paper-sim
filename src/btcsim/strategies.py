"""売買戦略。すべて「確定足のみ」を入力とし、未来の価格は参照しない。

共通ルール（戦略間で不公平が出ないよう明文化）
  * 判定に使うのは直近の確定足まで。進行中の足は使わない。
  * 買いは資金の order_pct（既定 20% = 1回あたり上限）を best_ask 側で成行。
  * 売りは保有 BTC 全量を best_bid 側で成行（部分利確はしない）。
  * 同一シグナル（同じ足・同じ向き）では 1 回だけ発注する（signal_key で判定）。
  * 指標が成立しない（データ不足）間はシグナルを出さない＝取引しない。
  * D (Buy & Hold) のみベンチマーク目的で 1 回だけ全額購入し、以後売却しない。
    このため D だけは 1 回 20% / BTC 比率 80% の上限を適用しない（README 参照）。
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from . import candles, indicators, ticks as tickmod


@dataclass
class Signal:
    action: str          # "BUY" | "SELL" | "HOLD"
    key: str             # 重複発注防止キー
    reason: str
    order_pct: Decimal = Decimal(0)
    note: str = ""
    order_jpy: Decimal | None = None   # 金額指定で買う戦略用（E）。None なら order_pct
    exit_kind: str = ""                # "take_profit" | "stop_loss" | "timeout" | "trend_reversal"


def _hold(reason: str) -> Signal:
    return Signal(action="HOLD", key="", reason=reason)


def strategy_a(cfg: dict, bars: dict[str, list[dict[str, Any]]], now_ts: int, pf, ctx: dict | None = None) -> Signal:
    p = cfg["strategies"]["A_ma_cross"]
    tf = p["timeframe"]
    cb = candles.closed_bars(bars.get(tf, []), now_ts, tf)
    cl = candles.closes(cb)
    need = int(p["long_window"]) + 1
    if len(cl) < need:
        return _hold(f"warmup: {len(cl)}/{need} closed {tf} bars")
    s_now = indicators.sma(cl, int(p["short_window"]))
    l_now = indicators.sma(cl, int(p["long_window"]))
    s_prev = indicators.sma(cl[:-1], int(p["short_window"]))
    l_prev = indicators.sma(cl[:-1], int(p["long_window"]))
    bar_ts = cb[-1]["ts"]
    base = f"A:{tf}:{bar_ts}"
    if s_prev <= l_prev and s_now > l_now:
        return Signal("BUY", f"{base}:BUY", f"golden cross short={s_now:.0f} long={l_now:.0f}", p["order_pct"])
    if s_prev >= l_prev and s_now < l_now:
        return Signal("SELL", f"{base}:SELL", f"dead cross short={s_now:.0f} long={l_now:.0f}")
    return _hold(f"no cross (short={s_now:.0f}, long={l_now:.0f})")


def strategy_b(cfg: dict, bars: dict[str, list[dict[str, Any]]], now_ts: int, pf, ctx: dict | None = None) -> Signal:
    p = cfg["strategies"]["B_rsi_reversal"]
    tf = p["timeframe"]
    period = int(p["rsi_period"])
    cb = candles.closed_bars(bars.get(tf, []), now_ts, tf)
    cl = candles.closes(cb)
    need = period + 2
    if len(cl) < need:
        return _hold(f"warmup: {len(cl)}/{need} closed {tf} bars")
    r_now = indicators.rsi(cl, period)
    r_prev = indicators.rsi(cl[:-1], period)
    bar_ts = cb[-1]["ts"]
    base = f"B:{tf}:{bar_ts}"
    if r_prev > p["buy_below"] >= r_now:
        return Signal("BUY", f"{base}:BUY", f"RSI entered oversold ({r_now:.2f})", p["order_pct"])
    if r_prev < p["sell_above"] <= r_now:
        return Signal("SELL", f"{base}:SELL", f"RSI entered overbought ({r_now:.2f})")
    return _hold(f"RSI={r_now:.2f} (no crossing)")


def strategy_c(cfg: dict, bars: dict[str, list[dict[str, Any]]], now_ts: int, pf, ctx: dict | None = None) -> Signal:
    p = cfg["strategies"]["C_combo"]
    ttf, etf = p["trend_timeframe"], p["entry_timeframe"]
    period = int(p["rsi_period"])
    tb = candles.closed_bars(bars.get(ttf, []), now_ts, ttf)
    eb = candles.closed_bars(bars.get(etf, []), now_ts, etf)
    tcl, ecl = candles.closes(tb), candles.closes(eb)
    need_t, need_e = int(p["trend_ma_window"]), period + 2
    if len(tcl) < need_t or len(ecl) < need_e:
        return _hold(f"warmup: trend {len(tcl)}/{need_t} {ttf}, entry {len(ecl)}/{need_e} {etf}")
    ma = indicators.sma(tcl, int(p["trend_ma_window"]))
    uptrend = tcl[-1] > ma
    r_now = indicators.rsi(ecl, period)
    r_prev = indicators.rsi(ecl[:-1], period)
    bar_ts = eb[-1]["ts"]
    base = f"C:{etf}:{bar_ts}"
    trend_txt = "up" if uptrend else "down"
    if uptrend and r_prev > p["buy_below"] >= r_now:
        return Signal("BUY", f"{base}:BUY", f"uptrend(1h MA {ma:.0f}) + RSI dip {r_now:.2f}", p["order_pct"])
    if not uptrend and pf.btc > 0:
        return Signal("SELL", f"{base}:SELL:trend", f"trend turned down (close {tcl[-1]:.0f} < MA {ma:.0f})")
    if r_prev < p["sell_above"] <= r_now and pf.btc > 0:
        return Signal("SELL", f"{base}:SELL:rsi", f"RSI overbought {r_now:.2f}")
    return _hold(f"trend={trend_txt}, RSI={r_now:.2f}")


def strategy_d(cfg: dict, bars: dict[str, list[dict[str, Any]]], now_ts: int, pf, ctx: dict | None = None) -> Signal:
    """開始時に一括購入し、以後は何もしない比較用ベンチマーク。"""
    p = cfg["strategies"]["D_buy_and_hold"]
    if pf.bootstrapped or pf.btc > 0:
        return _hold("buy & hold: holding (no further trades)")
    return Signal("BUY", "D:initial", "initial buy & hold allocation", p["allocation_pct"], note="benchmark")


def strategy_e(cfg: dict, bars: dict[str, list[dict[str, Any]]], now_ts: int, pf,
               ctx: dict | None = None) -> Signal:
    """E: 短期モメンタム（数十分〜数時間の順張りスキャルピング）。

    ローソク足ではなく 10 分ごとのティック列から実時間の変化率を計算する。
    エントリー条件（すべて満たしたとき買い）:
      1. 直近 lookback_short_minutes の上昇率 >= entry_momentum_pct
      2. 直近 lookback_long_minutes の変化率 > entry_long_momentum_pct
      3. 現在のスプレッドが entry_max_spread_pct 以内
      4. BTC を保有していない（同時 1 ポジション）
      5. 直近の売却から cooldown_minutes 以上経過（決済直後の再購入禁止）
    エグジット条件（いずれか）:
      利確 +take_profit_pct / 損切り -stop_loss_pct /
      最大保有 max_hold_minutes 超過 / 短期トレンド反転（短期変化率 <= trend_reversal_pct）
    判定価格は「評価時点で売れる価格（best_bid）」を使い、実際の約定は
    その時点の板を歩いた VWAP + スリッページで行う（指値どおりの約定は仮定しない）。
    データ不足・ティック欠落時は一切売買しない。
    """
    p = cfg["strategies"]["E_short_momentum"]
    ctx = ctx or {}
    series: list[dict[str, Any]] = ctx.get("ticks") or []
    bid: Decimal | None = ctx.get("best_bid")
    spread_pct: Decimal | None = ctx.get("spread_pct")

    short_min = int(p["lookback_short_minutes"])
    long_min = int(p["lookback_long_minutes"])

    # --- 保有中: エグジット判定を最優先（データ不足でも時間切れは効かせる） ---
    if pf.btc > 0 and pf.position_entry_price:
        entry = Decimal(str(pf.position_entry_price))
        held_min = (now_ts - int(pf.position_entry_ts or now_ts)) / 60
        key = f"E:exit:{pf.position_entry_ts}"
        change = (bid - entry) / entry if (bid and entry > 0) else None
        if change is not None and change >= p["take_profit_pct"]:
            return Signal("SELL", f"{key}:tp", f"take profit: {change:+.2%} (entry {entry:.0f} -> bid {bid:.0f})",
                          exit_kind="take_profit")
        if change is not None and change <= -p["stop_loss_pct"]:
            return Signal("SELL", f"{key}:sl", f"stop loss: {change:+.2%} (entry {entry:.0f} -> bid {bid:.0f})",
                          exit_kind="stop_loss")
        if held_min >= int(p["max_hold_minutes"]):
            return Signal("SELL", f"{key}:timeout", f"max hold {held_min:.0f}min reached ({change:+.2%})"
                          if change is not None else f"max hold {held_min:.0f}min reached",
                          exit_kind="timeout")
        mom_s = tickmod.change_pct(series, now_ts, short_min)
        if mom_s is not None and mom_s <= p["trend_reversal_pct"]:
            return Signal("SELL", f"{key}:rev", f"trend reversal: {short_min}min {mom_s:+.2%}",
                          exit_kind="trend_reversal")
        return _hold(f"holding {held_min:.0f}min, pnl {change:+.2%}" if change is not None
                     else f"holding {held_min:.0f}min")

    if pf.btc > 0:          # 想定外（建値不明）なので何もしない
        return _hold("holding without entry price record; no action")

    # --- 未保有: エントリー判定 ---
    covered = tickmod.covered_minutes(series, now_ts)
    if covered < long_min:
        return _hold(f"warmup: tick history {covered:.0f}min/{long_min}min")
    gap = tickmod.max_gap_minutes(series, now_ts, long_min)
    if gap > int(p["max_data_gap_minutes"]):
        return _hold(f"data gap {gap:.0f}min > {p['max_data_gap_minutes']}min; no trade")

    cooldown = int(p["cooldown_minutes"])
    if pf.last_exit_ts and (now_ts - int(pf.last_exit_ts)) / 60 < cooldown:
        left = cooldown - (now_ts - int(pf.last_exit_ts)) / 60
        return _hold(f"cooldown {left:.0f}min left after last exit")

    mom_s = tickmod.change_pct(series, now_ts, short_min)
    mom_l = tickmod.change_pct(series, now_ts, long_min)
    if mom_s is None or mom_l is None:
        return _hold("momentum unavailable (reference tick missing)")
    if spread_pct is not None and spread_pct > p["entry_max_spread_pct"]:
        return _hold(f"spread {spread_pct:.3%} > {p['entry_max_spread_pct']:.3%}; no trade")
    if mom_s < p["entry_momentum_pct"]:
        return _hold(f"{short_min}min momentum {mom_s:+.2%} < {p['entry_momentum_pct']:+.2%}")
    if mom_l <= p["entry_long_momentum_pct"]:
        return _hold(f"{long_min}min change {mom_l:+.2%} not positive")

    bucket = candles.bucket_start(now_ts, 10)
    return Signal("BUY", f"E:entry:{bucket}",
                  f"momentum {short_min}min {mom_s:+.2%}, {long_min}min {mom_l:+.2%}",
                  order_jpy=p["order_jpy"])


REGISTRY = {
    "A_ma_cross": strategy_a,
    "B_rsi_reversal": strategy_b,
    "C_combo": strategy_c,
    "D_buy_and_hold": strategy_d,
    "E_short_momentum": strategy_e,
}
