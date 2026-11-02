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

from . import candles, indicators


@dataclass
class Signal:
    action: str          # "BUY" | "SELL" | "HOLD"
    key: str             # 重複発注防止キー
    reason: str
    order_pct: Decimal = Decimal(0)
    note: str = ""


def _hold(reason: str) -> Signal:
    return Signal(action="HOLD", key="", reason=reason)


def strategy_a(cfg: dict, bars: dict[str, list[dict[str, Any]]], now_ts: int, pf) -> Signal:
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


def strategy_b(cfg: dict, bars: dict[str, list[dict[str, Any]]], now_ts: int, pf) -> Signal:
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


def strategy_c(cfg: dict, bars: dict[str, list[dict[str, Any]]], now_ts: int, pf) -> Signal:
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


def strategy_d(cfg: dict, bars: dict[str, list[dict[str, Any]]], now_ts: int, pf) -> Signal:
    """開始時に一括購入し、以後は何もしない比較用ベンチマーク。"""
    p = cfg["strategies"]["D_buy_and_hold"]
    if pf.bootstrapped or pf.btc > 0:
        return _hold("buy & hold: holding (no further trades)")
    return Signal("BUY", "D:initial", "initial buy & hold allocation", p["allocation_pct"], note="benchmark")


REGISTRY = {
    "A_ma_cross": strategy_a,
    "B_rsi_reversal": strategy_b,
    "C_combo": strategy_c,
    "D_buy_and_hold": strategy_d,
}
