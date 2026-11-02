"""市場データ取得。bitFlyer Public API のみ（認証不要・読み取り専用）。

実装しているのは公開エンドポイントだけであり、注文・残高・送金などの
Private API は一切呼ばない（APIキーも受け取らない）。

- 価格/板         : https://api.bitflyer.com/v1/getticker, /v1/getboard
- 直近約定        : https://api.bitflyer.com/v1/getexecutions
- ウォームアップ用 : bitbank public candlestick（BTC/JPY の 1 分足・無認証）
  bitFlyer はローソク足APIを公開していないため、実験開始直後に指標を成立
  させるための「過去足」だけ別取引所の公開1分足を代理データとして使う。
  売買に使う価格（best_bid/best_ask）は常に bitFlyer のものを使用する。
"""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

BITFLYER = "https://api.bitflyer.com/v1"
BITBANK = "https://public.bitbank.cc"
USER_AGENT = "btc-paper-sim/1.0 (+https://github.com/)"


class ApiError(RuntimeError):
    """市場データ取得に失敗したことを示す。発生時は売買を行わない。"""


@dataclass
class ApiStats:
    requests: int = 0
    errors: int = 0
    messages: list[str] = field(default_factory=list)

    def note(self, msg: str) -> None:
        self.errors += 1
        self.messages.append(msg)


def _get(url: str, stats: ApiStats, timeout: float = 10.0, retries: int = 3) -> Any:
    last: Exception | None = None
    for attempt in range(retries):
        stats.requests += 1
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status != 200:
                    raise ApiError(f"HTTP {resp.status} for {url}")
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError, ApiError) as exc:
            last = exc
            stats.note(f"{type(exc).__name__}: {exc} ({url})")
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
    raise ApiError(f"request failed after {retries} attempts: {last}")


# --- bitFlyer -------------------------------------------------------------

def get_ticker(product_code: str, stats: ApiStats) -> dict[str, Any]:
    data = _get(f"{BITFLYER}/getticker?product_code={product_code}", stats)
    for key in ("ltp", "best_bid", "best_ask", "timestamp"):
        if key not in data:
            raise ApiError(f"ticker missing field: {key}")
    return {
        "ltp": Decimal(str(data["ltp"])),
        "best_bid": Decimal(str(data["best_bid"])),
        "best_ask": Decimal(str(data["best_ask"])),
        "best_bid_size": Decimal(str(data.get("best_bid_size", 0))),
        "best_ask_size": Decimal(str(data.get("best_ask_size", 0))),
        "volume_24h": Decimal(str(data.get("volume_by_product", 0))),
        "exchange_timestamp": data["timestamp"],
    }


def get_board(product_code: str, stats: ApiStats, depth: int = 30) -> dict[str, list[tuple[Decimal, Decimal]]]:
    data = _get(f"{BITFLYER}/getboard?product_code={product_code}", stats)
    if not data.get("bids") or not data.get("asks"):
        raise ApiError("board is empty")
    bids = sorted(((Decimal(str(b["price"])), Decimal(str(b["size"]))) for b in data["bids"]), reverse=True)
    asks = sorted((Decimal(str(a["price"])), Decimal(str(a["size"]))) for a in data["asks"])
    return {"bids": bids[:depth], "asks": asks[:depth]}


def get_executions(product_code: str, stats: ApiStats, count: int = 500) -> list[dict[str, Any]]:
    data = _get(f"{BITFLYER}/getexecutions?product_code={product_code}&count={count}", stats)
    out = []
    for e in data:
        try:
            out.append({
                "price": Decimal(str(e["price"])),
                "size": Decimal(str(e["size"])),
                "exec_date": e["exec_date"],
            })
        except (KeyError, TypeError):
            continue
    return out


# --- ウォームアップ用の過去足（代理データ源） ------------------------------

def get_bitbank_1m(yyyymmdd: str, stats: ApiStats) -> list[dict[str, Any]]:
    """bitbank の BTC/JPY 1分足を取得。失敗しても例外は投げず空リストを返す。"""
    url = f"{BITBANK}/btc_jpy/candlestick/1min/{yyyymmdd}"
    try:
        data = _get(url, stats, retries=2)
    except ApiError:
        return []
    if data.get("success") != 1:
        stats.note(f"bitbank candlestick failed: {data}")
        return []
    bars: list[dict[str, Any]] = []
    for cs in data.get("data", {}).get("candlestick", []):
        for o, h, l, c, v, ts in cs.get("ohlcv", []):
            bars.append({
                "ts_ms": int(ts),
                "open": Decimal(str(o)), "high": Decimal(str(h)),
                "low": Decimal(str(l)), "close": Decimal(str(c)),
                "volume": Decimal(str(v)),
            })
    bars.sort(key=lambda b: b["ts_ms"])
    return bars
