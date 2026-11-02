"""ローソク足の生成・管理。

10 分ごとのスナップショットだけでは高値/安値/出来高を再現できないため、
毎回 bitFlyer の公開約定履歴 (getexecutions) を取得し、約定単位で足を更新
する。約定履歴は直近数百件しか取得できないので、取得できなかった区間は
ティック価格で補完する。この精度の限界は README に明記している。
"""
from __future__ import annotations

from decimal import Decimal
from typing import Any, Iterable

TIMEFRAMES = {"30m": 30, "1h": 60}


def tf_minutes(tf: str) -> int:
    if tf not in TIMEFRAMES:
        raise ValueError(f"unsupported timeframe: {tf}")
    return TIMEFRAMES[tf]


def bucket_start(ts: int, minutes: int) -> int:
    """epoch 秒 ts を足の開始時刻（epoch 秒）に丸める。"""
    size = minutes * 60
    return (int(ts) // size) * size


def update_bars(
    bars: list[dict[str, Any]],
    samples: Iterable[tuple[int, Decimal, Decimal]],
    tf: str,
    max_bars: int = 1200,
) -> list[dict[str, Any]]:
    """(epoch秒, 価格, 出来高) のサンプル列で足を更新して返す。"""
    minutes = tf_minutes(tf)
    index = {int(b["ts"]): b for b in bars}
    for ts, price, size in sorted(samples, key=lambda s: s[0]):
        key = bucket_start(ts, minutes)
        bar = index.get(key)
        if bar is None:
            index[key] = {
                "ts": key, "o": str(price), "h": str(price),
                "l": str(price), "c": str(price), "v": str(size),
            }
            continue
        bar["h"] = str(max(Decimal(bar["h"]), price))
        bar["l"] = str(min(Decimal(bar["l"]), price))
        bar["c"] = str(price)
        bar["v"] = str(Decimal(bar["v"]) + size)
    out = [index[k] for k in sorted(index)]
    return out[-max_bars:]


def closed_bars(bars: list[dict[str, Any]], now_ts: int, tf: str) -> list[dict[str, Any]]:
    """確定済み（期間が終了した）足のみを返す。ルックアヘッド防止の中心。"""
    size = tf_minutes(tf) * 60
    return [b for b in bars if int(b["ts"]) + size <= int(now_ts)]


def closes(bars: list[dict[str, Any]]) -> list[Decimal]:
    return [Decimal(b["c"]) for b in bars]


def aggregate_1m(bars_1m: list[dict[str, Any]], tf: str) -> list[dict[str, Any]]:
    """1 分足（api.get_bitbank_1m 形式）を指定足に集約する。"""
    minutes = tf_minutes(tf)
    index: dict[int, dict[str, Any]] = {}
    for b in sorted(bars_1m, key=lambda x: x["ts_ms"]):
        key = bucket_start(int(b["ts_ms"]) // 1000, minutes)
        cur = index.get(key)
        if cur is None:
            index[key] = {
                "ts": key, "o": str(b["open"]), "h": str(b["high"]),
                "l": str(b["low"]), "c": str(b["close"]), "v": str(b["volume"]),
            }
        else:
            cur["h"] = str(max(Decimal(cur["h"]), b["high"]))
            cur["l"] = str(min(Decimal(cur["l"]), b["low"]))
            cur["c"] = str(b["close"])
            cur["v"] = str(Decimal(cur["v"]) + b["volume"])
    return [index[k] for k in sorted(index)]


def merge_bars(existing: list[dict[str, Any]], incoming: list[dict[str, Any]], max_bars: int = 1200) -> list[dict[str, Any]]:
    """既存の足を優先して不足分だけ補う（自前の足を代理データで壊さない）。"""
    index = {int(b["ts"]): b for b in incoming}
    index.update({int(b["ts"]): b for b in existing})
    out = [index[k] for k in sorted(index)]
    return out[-max_bars:]
