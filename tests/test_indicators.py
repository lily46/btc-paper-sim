from decimal import Decimal

from btcsim import candles, indicators

D = Decimal


def test_sma_basic():
    assert indicators.sma([D(1), D(2), D(3)], 3) == D(2)
    assert indicators.sma([D(1), D(2)], 3) is None
    assert indicators.sma([D(1), D(2), D(3), D(10)], 2) == Decimal("6.5")


def test_rsi_all_up_is_100():
    vals = [D(i) for i in range(1, 30)]
    assert indicators.rsi(vals, 14) == D(100)


def test_rsi_all_down_is_zero():
    vals = [D(i) for i in range(40, 10, -1)]
    assert indicators.rsi(vals, 14) == D(0)


def test_rsi_needs_enough_data():
    assert indicators.rsi([D(1), D(2)], 14) is None


def test_rsi_known_value():
    # Wilder RSI: 交互に +2 / -1 で推移すると RSI > 50 になる
    vals = [D(100)]
    for i in range(30):
        vals.append(vals[-1] + (D(2) if i % 2 == 0 else D(-1)))
    r = indicators.rsi(vals, 14)
    assert D(55) < r < D(80)


def test_bucket_start_and_closed_bars():
    assert candles.bucket_start(1_700_000_000, 30) % 1800 == 0
    bars = [{"ts": 0, "o": "1", "h": "1", "l": "1", "c": "1", "v": "0"},
            {"ts": 1800, "o": "1", "h": "1", "l": "1", "c": "1", "v": "0"}]
    assert len(candles.closed_bars(bars, 1800, "30m")) == 1     # 進行中の足は除外
    assert len(candles.closed_bars(bars, 3600, "30m")) == 2


def test_update_bars_tracks_ohlcv():
    samples = [(0, D(100), D(1)), (60, D(120), D(2)), (120, D(90), D(1)), (180, D(110), D(1))]
    bars = candles.update_bars([], samples, "30m")
    assert len(bars) == 1
    b = bars[0]
    assert (b["o"], b["h"], b["l"], b["c"], b["v"]) == ("100", "120", "90", "110", "5")


def test_update_bars_splits_buckets():
    samples = [(0, D(100), D(1)), (1900, D(200), D(1))]
    bars = candles.update_bars([], samples, "30m")
    assert [b["ts"] for b in bars] == [0, 1800]


def test_aggregate_1m_and_merge():
    bars_1m = [{"ts_ms": i * 60_000, "open": D(100 + i), "high": D(110 + i),
                "low": D(90 + i), "close": D(105 + i), "volume": D(1)} for i in range(60)]
    agg30 = candles.aggregate_1m(bars_1m, "30m")
    assert len(agg30) == 2
    assert agg30[0]["o"] == "100" and agg30[0]["v"] == "30"
    # 既存足は代理データに上書きされない
    mine = [{"ts": 0, "o": "1", "h": "1", "l": "1", "c": "1", "v": "9"}]
    merged = candles.merge_bars(mine, agg30)
    assert merged[0]["v"] == "9"
    assert len(merged) == 2
