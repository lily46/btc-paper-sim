"""証拠金取引（bitFlyer Lightning FX）の仮想口座モデル。

現物（`portfolio.Portfolio`）との違い:
  * **売りから入れる**（ショート）。建玉は数量 + 方向で持つ。
  * 現物のような「BTC 残高」は存在しない。あるのは証拠金と建玉。
  * レバレッジがあり、**証拠金維持率を割ると強制ロスカット**される。
  * 建玉管理料（スワップ）が日次で発生する。
  * bitFlyer の FX は取引手数料が無料（既定 0%、設定で変更可）。

評価は常に「いま決済したらいくらか」で行う（保守的）:
  * ロングの評価・決済は `best_bid` 側
  * ショートの評価・決済は `best_ask` 側

実注文は一切行わない。ここにあるのは計算だけ。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from decimal import Decimal
from typing import Any

from ..portfolio import JPY_Q, floor_size, q_jpy, walk_book

LONG = "LONG"
SHORT = "SHORT"


@dataclass
class FxTrade:
    """1 回の建玉操作（新規 or 決済）の記録。"""
    ts: str
    strategy: str
    action: str              # "OPEN" | "CLOSE"
    side: str                # LONG | SHORT（決済時は決済した建玉の方向）
    price: str
    size: str
    notional_jpy: str
    fee_jpy: str
    swap_jpy: str            # 決済時のみ: その建玉で払った建玉管理料の累計
    collateral_before: str
    collateral_after: str
    realized_pnl: str
    equity_after: str
    margin_ratio: str
    leverage: str
    spread_pct: str = ""
    slippage_pct: str = ""
    hold_minutes: str = ""
    entry_price: str = ""
    entry_reason: str = ""
    exit_kind: str = ""      # take_profit / stop_loss / timeout / reversal / liquidation
    signal_key: str = ""
    reason: str = ""


@dataclass
class Position:
    side: str
    size: str                # BTC（文字列で保持して Decimal 化する）
    entry_price: str
    entry_ts: int
    entry_reason: str = ""
    swap_paid: str = "0"
    last_swap_ts: int = 0

    def size_d(self) -> Decimal:
        return Decimal(self.size)

    def entry_d(self) -> Decimal:
        return Decimal(self.entry_price)


@dataclass
class MarginAccount:
    """1 戦略ぶんの仮想証拠金口座。"""
    strategy_id: str
    initial_jpy: Decimal
    collateral_jpy: Decimal
    position: dict[str, Any] | None = None     # Position を dict で保持（JSON 化のため）
    realized_pnl: Decimal = Decimal(0)
    fees_paid: Decimal = Decimal(0)
    swap_paid: Decimal = Decimal(0)
    trades: int = 0
    opens: int = 0
    closes: int = 0
    longs: int = 0
    shorts: int = 0
    wins: int = 0
    losses: int = 0
    liquidations: int = 0
    peak_equity: Decimal = Decimal(0)
    max_drawdown: Decimal = Decimal(0)
    halted: bool = False
    halt_reason: str = ""
    last_signal_key: str = ""
    last_exit_ts: int | None = None
    hold_minutes_total: str = "0"

    # --- 建玉 ---
    def pos(self) -> Position | None:
        return Position(**self.position) if self.position else None

    def unrealized(self, bid: Decimal, ask: Decimal) -> Decimal:
        """いま決済した場合の損益（手数料前）。"""
        p = self.pos()
        if p is None:
            return Decimal(0)
        if p.side == LONG:
            return q_jpy(p.size_d() * (bid - p.entry_d()))
        return q_jpy(p.size_d() * (p.entry_d() - ask))

    def exit_price_ref(self, bid: Decimal, ask: Decimal) -> Decimal | None:
        p = self.pos()
        if p is None:
            return None
        return bid if p.side == LONG else ask

    def equity(self, bid: Decimal, ask: Decimal) -> Decimal:
        """有効証拠金 = 証拠金 + 評価損益。"""
        return q_jpy(self.collateral_jpy + self.unrealized(bid, ask))

    def notional(self, bid: Decimal, ask: Decimal) -> Decimal:
        p = self.pos()
        if p is None:
            return Decimal(0)
        ref = self.exit_price_ref(bid, ask) or Decimal(0)
        return q_jpy(p.size_d() * ref)

    def required_margin(self, bid: Decimal, ask: Decimal, leverage: Decimal) -> Decimal:
        n = self.notional(bid, ask)
        return q_jpy(n / leverage) if n > 0 else Decimal(0)

    def margin_ratio(self, bid: Decimal, ask: Decimal, leverage: Decimal) -> Decimal | None:
        """証拠金維持率。建玉が無ければ None。"""
        req = self.required_margin(bid, ask, leverage)
        if req <= 0:
            return None
        return self.equity(bid, ask) / req

    def pnl_pct(self, bid: Decimal, ask: Decimal) -> Decimal | None:
        """建値に対する値動きの率（レバレッジ前）。利確/損切り判定に使う。"""
        p = self.pos()
        if p is None or p.entry_d() <= 0:
            return None
        ref = self.exit_price_ref(bid, ask)
        if p.side == LONG:
            return (ref - p.entry_d()) / p.entry_d()
        return (p.entry_d() - ref) / p.entry_d()

    def hold_minutes(self, now_ts: int) -> Decimal | None:
        p = self.pos()
        if p is None:
            return None
        return (Decimal(now_ts) - Decimal(p.entry_ts)) / Decimal(60)

    # --- 評価更新 ---
    def mark(self, bid: Decimal, ask: Decimal, dd_stop: Decimal) -> None:
        eq = self.equity(bid, ask)
        if eq > self.peak_equity:
            self.peak_equity = eq
        if self.peak_equity > 0:
            dd = (self.peak_equity - eq) / self.peak_equity
            if dd > self.max_drawdown:
                self.max_drawdown = dd
            if dd >= dd_stop and not self.halted:
                self.halted = True
                self.halt_reason = f"max drawdown {dd:.4f} >= {dd_stop}"

    def accrue_swap(self, now_ts: int, bid: Decimal, ask: Decimal, rate_per_day: Decimal) -> Decimal:
        """建玉管理料を経過時間に比例して証拠金から引く。

        bitFlyer は 1 日 1 回まとめて課金するが、10 分ごとの評価で
        資産推移を滑らかに見せるため按分して積み上げる（合計は同じ）。
        """
        p = self.pos()
        if p is None or rate_per_day <= 0:
            return Decimal(0)
        last = int(p.last_swap_ts or p.entry_ts)
        elapsed = max(0, now_ts - last)
        if elapsed <= 0:
            return Decimal(0)
        notional = self.notional(bid, ask)
        cost = q_jpy(notional * rate_per_day * Decimal(elapsed) / Decimal(86400))
        if cost <= 0:
            return Decimal(0)
        self.collateral_jpy -= cost
        self.swap_paid += cost
        p.swap_paid = str(Decimal(p.swap_paid) + cost)
        p.last_swap_ts = now_ts
        self.position = asdict(p)
        return cost

    # --- 建玉操作 ---
    def open_position(
        self, *, ts: str, side: str, notional_jpy: Decimal,
        board: dict[str, list[tuple[Decimal, Decimal]]], bid: Decimal, ask: Decimal,
        cfg: dict, signal_key: str, reason: str, now_ts: int,
        spread_pct: Decimal | None = None,
    ) -> FxTrade | None:
        if self.position is not None:
            return None                        # 同時 1 建玉（ドテンは先に決済する）
        risk, fees = cfg["risk"], cfg["fees"]
        leverage = risk["leverage"]
        slip = fees["slippage_bps"] / Decimal(10000)
        fee_rate = fees["taker_fee_rate"]

        # 建玉上限: 証拠金 × レバレッジ
        max_notional = self.collateral_jpy * leverage
        notional = min(notional_jpy, max_notional)
        if notional <= 0:
            return None

        levels = board["asks"] if side == LONG else board["bids"]
        if not levels:
            return None
        ref = levels[0][0]
        filled, vwap = walk_book(levels, notional / ref)
        if filled <= 0:
            return None
        price = q_jpy(vwap * (Decimal(1) + slip) if side == LONG else vwap * (Decimal(1) - slip))
        size = floor_size(min(filled, notional / price), risk["size_precision"])
        if size < risk["min_order_size_btc"]:
            return None

        gross = q_jpy(size * price)
        fee = q_jpy(gross * fee_rate)
        # 必要証拠金 + 手数料を払えるか
        if gross / leverage + fee > self.collateral_jpy:
            return None

        before = self.collateral_jpy
        self.collateral_jpy -= fee
        self.fees_paid += fee
        self.position = asdict(Position(
            side=side, size=str(size), entry_price=str(price), entry_ts=now_ts,
            entry_reason=reason, swap_paid="0", last_swap_ts=now_ts))
        self.trades += 1
        self.opens += 1
        if side == LONG:
            self.longs += 1
        else:
            self.shorts += 1
        self.last_signal_key = signal_key
        slip_actual = ((price / ref - Decimal(1)) if side == LONG else (Decimal(1) - price / ref))
        ratio = self.margin_ratio(bid, ask, leverage)
        return FxTrade(
            ts=ts, strategy=self.strategy_id, action="OPEN", side=side, price=str(price),
            size=str(size), notional_jpy=str(gross), fee_jpy=str(fee), swap_jpy="0",
            collateral_before=str(before), collateral_after=str(self.collateral_jpy),
            realized_pnl="0", equity_after=str(self.equity(bid, ask)),
            margin_ratio=str(ratio) if ratio is not None else "", leverage=str(leverage),
            spread_pct=str(spread_pct) if spread_pct is not None else "",
            slippage_pct=str(slip_actual), entry_price=str(price), entry_reason=reason,
            signal_key=signal_key, reason=reason,
        )

    def close_position(
        self, *, ts: str, board: dict[str, list[tuple[Decimal, Decimal]]],
        bid: Decimal, ask: Decimal, cfg: dict, signal_key: str, reason: str,
        now_ts: int, exit_kind: str, spread_pct: Decimal | None = None,
    ) -> FxTrade | None:
        p = self.pos()
        if p is None:
            return None
        risk, fees = cfg["risk"], cfg["fees"]
        leverage = risk["leverage"]
        slip = fees["slippage_bps"] / Decimal(10000)
        fee_rate = fees["taker_fee_rate"]

        # ロングは売り板へ、ショートは買い板へぶつける
        levels = board["bids"] if p.side == LONG else board["asks"]
        if not levels:
            return None
        ref = levels[0][0]
        filled, vwap = walk_book(levels, p.size_d())
        if filled <= 0:
            return None
        price = q_jpy(vwap * (Decimal(1) - slip) if p.side == LONG else vwap * (Decimal(1) + slip))
        size = floor_size(min(filled, p.size_d()), risk["size_precision"])
        if size <= 0:
            return None

        gross = q_jpy(size * price)
        fee = q_jpy(gross * fee_rate)
        gain = (size * (price - p.entry_d()) if p.side == LONG
                else size * (p.entry_d() - price))
        realized = q_jpy(gain - fee)

        before = self.collateral_jpy
        self.collateral_jpy += realized
        self.realized_pnl += realized
        self.fees_paid += fee
        self.trades += 1
        self.closes += 1
        if realized > 0:
            self.wins += 1
        elif realized < 0:
            self.losses += 1
        if exit_kind == "liquidation":
            self.liquidations += 1
        held = (Decimal(now_ts) - Decimal(p.entry_ts)) / Decimal(60)
        self.hold_minutes_total = str(Decimal(self.hold_minutes_total) + held)
        self.last_exit_ts = now_ts
        self.last_signal_key = signal_key
        swap_of_position = p.swap_paid
        entry_price, entry_reason = p.entry_price, p.entry_reason
        side = p.side
        self.position = None

        slip_actual = ((Decimal(1) - price / ref) if side == LONG else (price / ref - Decimal(1)))
        return FxTrade(
            ts=ts, strategy=self.strategy_id, action="CLOSE", side=side, price=str(price),
            size=str(size), notional_jpy=str(gross), fee_jpy=str(fee), swap_jpy=str(swap_of_position),
            collateral_before=str(before), collateral_after=str(self.collateral_jpy),
            realized_pnl=str(realized), equity_after=str(self.equity(bid, ask)),
            margin_ratio="", leverage=str(leverage),
            spread_pct=str(spread_pct) if spread_pct is not None else "",
            slippage_pct=str(slip_actual), hold_minutes=str(round(held, 2)),
            entry_price=entry_price, entry_reason=entry_reason, exit_kind=exit_kind,
            signal_key=signal_key, reason=reason,
        )

    def needs_liquidation(self, bid: Decimal, ask: Decimal, cfg: dict) -> bool:
        """証拠金維持率が下限を割ったか（bitFlyer のロスカットは 80% 未満）。"""
        if self.position is None:
            return False
        risk = cfg["risk"]
        ratio = self.margin_ratio(bid, ask, risk["leverage"])
        if ratio is None:
            return False
        return ratio < risk["liquidation_margin_ratio"] or self.equity(bid, ask) <= 0

    def avg_hold_minutes(self) -> Decimal | None:
        if not self.closes:
            return None
        return Decimal(self.hold_minutes_total) / Decimal(self.closes)

    # --- 永続化 ---
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in d.items()}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "MarginAccount":
        dec = {"initial_jpy", "collateral_jpy", "realized_pnl", "fees_paid",
               "swap_paid", "peak_equity", "max_drawdown"}
        known = {f.name for f in fields(cls)}
        kwargs = {k: (Decimal(str(v)) if k in dec else v)
                  for k, v in d.items() if k in known}
        return cls(**kwargs)
