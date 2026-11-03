"""仮想ポートフォリオと約定シミュレーション（現物のみ・Decimal 計算）。

- レバレッジ / 借入 / 空売りは実装自体が存在しない（btc < 0, jpy < 0 を作れない）
- 購入は best_ask 側、売却は best_bid 側の板を数量分だけ歩いて VWAP を求める
- さらに設定のスリッページ(bps)を不利方向に加算する
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from typing import Any

JPY_Q = Decimal("0.0001")


def q_jpy(v: Decimal) -> Decimal:
    return v.quantize(JPY_Q, rounding=ROUND_HALF_UP)


def floor_size(size: Decimal, precision: int) -> Decimal:
    return size.quantize(Decimal(1).scaleb(-precision), rounding=ROUND_DOWN)


def walk_book(levels: list[tuple[Decimal, Decimal]], size: Decimal) -> tuple[Decimal, Decimal]:
    """板を size 分歩いて (約定可能数量, VWAP) を返す。板が薄ければ数量を減らす。"""
    remaining = size
    cost = Decimal(0)
    filled = Decimal(0)
    for price, avail in levels:
        if remaining <= 0:
            break
        take = min(remaining, avail)
        cost += take * price
        filled += take
        remaining -= take
    if filled <= 0:
        return Decimal(0), Decimal(0)
    return filled, cost / filled


@dataclass
class Trade:
    ts: str
    strategy: str
    side: str
    price: str
    size: str
    gross_jpy: str
    fee_jpy: str
    jpy_before: str
    jpy_after: str
    btc_before: str
    btc_after: str
    realized_pnl: str
    equity_after: str
    signal_key: str
    reason: str
    spread_pct: str = ""          # 約定判定時のスプレッド
    slippage_pct: str = ""        # best 価格からの乖離（板歩き + スリッページ）
    hold_minutes: str = ""        # 売却時のみ: 保有時間
    entry_reason: str = ""        # 売却時のみ: その建玉のエントリー理由
    exit_kind: str = ""           # 売却時のみ: take_profit / stop_loss / timeout / trend_reversal
    entry_price: str = ""         # 売却時のみ: 建値


@dataclass
class Portfolio:
    strategy_id: str
    initial_jpy: Decimal
    jpy: Decimal
    btc: Decimal = Decimal(0)
    cost_basis_jpy: Decimal = Decimal(0)
    realized_pnl: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)
    trades: int = 0
    buys: int = 0
    sells: int = 0
    wins: int = 0
    losses: int = 0
    peak_equity: Decimal = Decimal(0)
    max_drawdown: Decimal = Decimal(0)
    halted: bool = False
    halt_reason: str = ""
    last_signal_key: str = ""
    bootstrapped: bool = False
    # --- ポジション情報（戦略 E の利確/損切り/時間切れ判定に使う） ---
    position_entry_ts: int | None = None
    position_entry_price: str | None = None
    position_entry_reason: str = ""
    last_exit_ts: int | None = None
    hold_minutes_total: str = "0"      # 平均保有時間の算出用（決済の合計保有分）

    # --- 評価 ---
    def equity(self, mark_price: Decimal) -> Decimal:
        return q_jpy(self.jpy + self.btc * mark_price)

    def unrealized_pnl(self, mark_price: Decimal) -> Decimal:
        return q_jpy(self.btc * mark_price - self.cost_basis_jpy)

    def avg_cost(self) -> Decimal:
        return q_jpy(self.cost_basis_jpy / self.btc) if self.btc > 0 else Decimal(0)

    def mark(self, mark_price: Decimal, dd_stop: Decimal) -> None:
        eq = self.equity(mark_price)
        if eq > self.peak_equity:
            self.peak_equity = eq
        if self.peak_equity > 0:
            dd = (self.peak_equity - eq) / self.peak_equity
            if dd > self.max_drawdown:
                self.max_drawdown = dd
            if dd >= dd_stop and not self.halted:
                self.halted = True
                self.halt_reason = f"max drawdown {dd:.4f} >= {dd_stop}"

    # --- 売買 ---
    def buy(
        self, *, ts: str, notional_jpy: Decimal, asks: list[tuple[Decimal, Decimal]],
        mark_price: Decimal, cfg: dict, signal_key: str, reason: str,
        now_ts: int | None = None, spread_pct: Decimal | None = None,
    ) -> Trade | None:
        risk, fees = cfg["risk"], cfg["fees"]
        fee_rate = fees["taker_fee_rate"]
        slip = fees["slippage_bps"] / Decimal(10000)

        max_order = self.initial_jpy * risk["max_order_pct_of_initial"]
        notional = min(notional_jpy, max_order)
        notional = min(notional, (self.jpy - risk["min_jpy_reserve"]) / (Decimal(1) + fee_rate))

        # BTC 比率上限（約定後の BTC 評価額 <= 総資産 * 上限）
        equity = self.equity(mark_price)
        room = equity * risk["max_btc_pct_of_equity"] - self.btc * mark_price
        notional = min(notional, room)
        if notional <= 0:
            return None

        ref_price = asks[0][0] * (Decimal(1) + slip)
        want_size = notional / ref_price
        filled, vwap = walk_book(asks, want_size)
        if filled <= 0:
            return None
        price = q_jpy(vwap * (Decimal(1) + slip))
        size = floor_size(min(filled, notional / price), risk["size_precision"])
        if size < risk["min_order_size_btc"]:
            return None

        gross = q_jpy(size * price)
        fee = q_jpy(gross * fee_rate)
        if gross + fee > self.jpy:  # 残高超過は禁止
            return None

        jpy_before, btc_before = self.jpy, self.btc
        self.jpy -= gross + fee
        self.btc += size
        self.cost_basis_jpy += gross + fee
        self.fees_paid += fee
        self.trades += 1
        self.buys += 1
        self.last_signal_key = signal_key
        if btc_before == 0:                      # 新規建玉
            self.position_entry_ts = now_ts
            self.position_entry_price = str(price)
            self.position_entry_reason = reason
        slip = (price / asks[0][0] - Decimal(1)) if asks and asks[0][0] > 0 else Decimal(0)
        return Trade(
            ts=ts, strategy=self.strategy_id, side="BUY", price=str(price), size=str(size),
            gross_jpy=str(gross), fee_jpy=str(fee), jpy_before=str(jpy_before), jpy_after=str(self.jpy),
            btc_before=str(btc_before), btc_after=str(self.btc), realized_pnl="0",
            equity_after=str(self.equity(mark_price)), signal_key=signal_key, reason=reason,
            spread_pct=str(spread_pct) if spread_pct is not None else "",
            slippage_pct=str(slip), entry_price=str(price),
        )

    def sell(
        self, *, ts: str, size_btc: Decimal, bids: list[tuple[Decimal, Decimal]],
        mark_price: Decimal, cfg: dict, signal_key: str, reason: str,
        now_ts: int | None = None, spread_pct: Decimal | None = None,
        exit_kind: str = "",
    ) -> Trade | None:
        risk, fees = cfg["risk"], cfg["fees"]
        fee_rate = fees["taker_fee_rate"]
        slip = fees["slippage_bps"] / Decimal(10000)

        size = floor_size(min(size_btc, self.btc), risk["size_precision"])  # 空売り禁止
        if size < risk["min_order_size_btc"]:
            return None
        filled, vwap = walk_book(bids, size)
        size = floor_size(filled, risk["size_precision"])
        if size < risk["min_order_size_btc"]:
            return None
        price = q_jpy(vwap * (Decimal(1) - slip))
        gross = q_jpy(size * price)
        fee = q_jpy(gross * fee_rate)
        proceeds = gross - fee

        avg = self.cost_basis_jpy / self.btc
        released_cost = q_jpy(avg * size)
        realized = q_jpy(proceeds - released_cost)

        jpy_before, btc_before = self.jpy, self.btc
        entry_price = self.position_entry_price or ""
        entry_reason = self.position_entry_reason
        held_min = ""
        if now_ts is not None and self.position_entry_ts:
            held_min = str(round((now_ts - int(self.position_entry_ts)) / 60, 2))
        self.jpy += proceeds
        self.btc -= size
        self.cost_basis_jpy = max(Decimal(0), self.cost_basis_jpy - released_cost)
        if self.btc == 0:
            self.cost_basis_jpy = Decimal(0)
        self.realized_pnl += realized
        self.fees_paid += fee
        self.trades += 1
        self.sells += 1
        if realized > 0:
            self.wins += 1
        elif realized < 0:
            self.losses += 1
        self.last_signal_key = signal_key
        if self.btc == 0:                        # 建玉を完全に閉じた
            if held_min:
                self.hold_minutes_total = str(Decimal(self.hold_minutes_total) + Decimal(held_min))
            self.last_exit_ts = now_ts
            self.position_entry_ts = None
            self.position_entry_price = None
            self.position_entry_reason = ""
        slip = (Decimal(1) - price / bids[0][0]) if bids and bids[0][0] > 0 else Decimal(0)
        return Trade(
            ts=ts, strategy=self.strategy_id, side="SELL", price=str(price), size=str(size),
            gross_jpy=str(gross), fee_jpy=str(fee), jpy_before=str(jpy_before), jpy_after=str(self.jpy),
            btc_before=str(btc_before), btc_after=str(self.btc), realized_pnl=str(realized),
            equity_after=str(self.equity(mark_price)), signal_key=signal_key, reason=reason,
            spread_pct=str(spread_pct) if spread_pct is not None else "",
            slippage_pct=str(slip), hold_minutes=held_min, entry_reason=entry_reason,
            exit_kind=exit_kind, entry_price=str(entry_price),
        )

    # --- 永続化 ---
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Portfolio":
        """旧バージョンの state（戦略 E 追加前）も読めるようにする。

        * 新しく増えたフィールドが無ければ dataclass の既定値を使う
        * 知らないフィールドが残っていても無視する
        """
        dec = {"initial_jpy", "jpy", "btc", "cost_basis_jpy", "realized_pnl",
               "fees_paid", "peak_equity", "max_drawdown"}
        known = {f.name for f in fields(cls)}
        kwargs = {k: (Decimal(str(v)) if k in dec else v)
                  for k, v in d.items() if k in known}
        return cls(**kwargs)

    def avg_hold_minutes(self) -> Decimal | None:
        """決済済み建玉の平均保有時間（分）。決済ゼロなら None。"""
        if not self.sells:
            return None
        return Decimal(self.hold_minutes_total) / Decimal(self.sells)
