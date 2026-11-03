"""10 分ごとのティック列（state["ticks"]）を扱うヘルパー。

戦略 E（短期モメンタム）は 30 分・60 分といった実時間の変化率を使うため、
ローソク足ではなく実際の取得時刻つきティック列を直接参照する。

ティックの時刻は新しい記録では `ts`（epoch 秒）を持つが、戦略 E 追加以前の
記録には無いため `tick_id`（10 分バケットの epoch 秒）にフォールバックする。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any


def tick_ts(tick: dict[str, Any]) -> int:
    """ティックの時刻（epoch 秒）。古い記録でも読めるようにする。"""
    if tick.get("ts") is not None:
        return int(tick["ts"])
    return int(tick["tick_id"])


def price_at(ticks: list[dict[str, Any]], target_ts: int, tolerance_s: int) -> Decimal | None:
    """target_ts に最も近いティックの LTP。許容誤差を超える場合は None。

    未来のティックは参照しない（ルックアヘッド防止）。
    """
    best: tuple[int, Decimal] | None = None
    for t in ticks:
        ts = tick_ts(t)
        if ts > target_ts + tolerance_s:
            continue
        diff = abs(ts - target_ts)
        if diff <= tolerance_s and (best is None or diff < best[0]):
            best = (diff, Decimal(str(t["ltp"])))
    return best[1] if best else None


def change_pct(ticks: list[dict[str, Any]], now_ts: int, minutes: int,
               tolerance_s: int = 420) -> Decimal | None:
    """minutes 分前からの価格変化率。基準ティックが無ければ None。"""
    if not ticks:
        return None
    latest = Decimal(str(ticks[-1]["ltp"]))
    past = price_at(ticks, now_ts - minutes * 60, tolerance_s)
    if past is None or past <= 0:
        return None
    return (latest - past) / past


def covered_minutes(ticks: list[dict[str, Any]], now_ts: int) -> float:
    """ティック列が現在からさかのぼって何分ぶんを覆っているか。"""
    if not ticks:
        return 0.0
    return (now_ts - tick_ts(ticks[0])) / 60.0


def max_gap_minutes(ticks: list[dict[str, Any]], now_ts: int, window_minutes: int) -> float:
    """直近 window_minutes の中で最大のティック欠落（分）。

    GitHub Actions のスケジュール欠落で穴が空いた区間を検出し、
    モメンタム計算が信用できない場合に取引を止めるために使う。
    """
    since = now_ts - window_minutes * 60
    stamps = [tick_ts(t) for t in ticks if tick_ts(t) >= since]
    if not stamps:
        return float(window_minutes)
    stamps.append(now_ts)
    gaps = [(b - a) / 60.0 for a, b in zip(stamps, stamps[1:])]
    head = (stamps[0] - since) / 60.0          # 窓の先頭が欠けている場合
    return max(gaps + [head]) if gaps else head
