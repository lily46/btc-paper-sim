"""最終レポートを Markdown / JSON / CSV で出力する。"""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .metrics import summarize

COLUMNS = [
    "strategy", "equity_jpy", "total_pnl_jpy", "total_pnl_pct", "jpy_balance",
    "btc_balance", "realized_pnl_jpy", "unrealized_pnl_jpy", "trades", "buys",
    "sells", "closed_trades", "win_rate_pct", "fees_jpy", "max_drawdown_pct",
    "vs_buy_and_hold_jpy", "halted", "halt_reason",
]


def build(root: Path | str, state: dict[str, Any], cfg: dict) -> dict[str, Path]:
    root = Path(root)
    out_dir = root / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(state, cfg)

    json_path = out_dir / "final_report.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    csv_path = out_dir / "final_report.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in summary["strategies"].values():
            w.writerow({c: row.get(c) for c in COLUMNS})

    md_path = out_dir / "final_report.md"
    md_path.write_text(_markdown(summary, state), encoding="utf-8")
    return {"json": json_path, "csv": csv_path, "md": md_path}


def _markdown(s: dict[str, Any], state: dict[str, Any]) -> str:
    exp, mkt, stats = s["experiment"], s["market"], s.get("stats", {})
    rows = s["strategies"]
    best = max(rows.values(), key=lambda r: Decimal(r["equity_jpy"])) if rows else None

    lines = [
        "# Bitcoin 自動売買シミュレーター 最終レポート",
        "",
        "> **これは仮想売買（ペーパートレード）の記録です。実際の注文・入出金は一切行っていません。**",
        "",
        f"- 生成時刻 (UTC): {s['generated_at_utc']}",
        f"- 実験開始 (JST): {exp['started_at_jst']}",
        f"- 実験終了予定 (JST): {exp['ends_at_jst']}",
        f"- 実験終了 (JST): {exp.get('finished_at_jst') or '（未終了）'}",
        f"- ステータス: {exp['status']}",
        f"- 通貨ペア: {exp['product_code']} / 初期資金: {exp['initial_capital_jpy']} 円 × 各戦略",
        "",
        "## BTC 価格",
        "",
        f"- 開始価格: {mkt['start_price']} 円",
        f"- 最終価格 (LTP): {mkt['ltp']} 円",
        f"- 騰落率: {mkt['price_change_pct']} %",
        f"- データ取得元: {mkt['source']}",
        f"- 最終取得時刻 (JST): {mkt['last_fetch_jst']}",
        "",
        "## 戦略別成績",
        "",
        "| 戦略 | 最終総資産(円) | 損益(円) | 損益率(%) | JPY残高 | BTC保有 | 実現損益 | 評価損益 | 売買回数 | 勝率(%) | 手数料(円) | 最大DD(%) | B&H差(円) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for sid in sorted(rows):
        r = rows[sid]
        lines.append(
            f"| {sid} | {r['equity_jpy']} | {r['total_pnl_jpy']} | {r['total_pnl_pct']} | "
            f"{r['jpy_balance']} | {r['btc_balance']} | {r['realized_pnl_jpy']} | "
            f"{r['unrealized_pnl_jpy']} | {r['trades']} | {r['win_rate_pct'] or '-'} | "
            f"{r['fees_jpy']} | {r['max_drawdown_pct']} | {r.get('vs_buy_and_hold_jpy', '-')} |"
        )

    total_fees = sum((Decimal(r["fees_jpy"]) for r in rows.values()), Decimal(0))
    lines += [
        "",
        f"- 全戦略の手数料総額: {total_fees} 円",
        f"- 最良戦略: {best['strategy'] if best else '-'}"
        + (f"（総資産 {best['equity_jpy']} 円）" if best else ""),
        "",
        "## 勝率の算出方法",
        "",
        "売却（決済）1 回を 1 トレードとみなし、手数料を含む実現損益がプラスだった割合",
        "（wins / (wins + losses)）。未決済ポジションと実現損益ちょうど 0 の決済は分母から除外。",
        "Buy & Hold は決済が発生しないため `-` と表示される。",
        "",
        "## 実行品質",
        "",
        f"- Workflow 実行回数: {stats.get('runs', 0)}",
        f"- 期待ティック数: {stats.get('expected_ticks', 0)} / 実際に処理したティック数: {stats.get('observed_ticks', 0)}",
        f"- スケジュール実行漏れ: {stats.get('missed_schedules', 0)} 回",
        f"- API エラー回数: {stats.get('api_errors', 0)} 回",
        f"- 異常値検出による売買スキップ: {stats.get('data_rejections', 0)} 回",
        f"- 重複ティックのスキップ（二重売買防止）: {stats.get('skipped_duplicate_ticks', 0)} 回",
        f"- 記録された仮想売買件数: {len(state.get('trades', []))}",
        "",
        "## 改善すべき点 / 既知の限界",
        "",
        "1. ローソク足は 10 分ごとの実行時に取得できる直近約定履歴から生成しているため、",
        "   実行間隔の空白区間の高値・安値・出来高は完全ではない。1 分足を常時収集する",
        "   別経路（WebSocket 常駐プロセス等）があれば精度が上がる。",
        "2. 実験開始直後の指標ウォームアップに別取引所（bitbank）の公開 1 分足を代理利用している。",
        "   取引所間で価格に僅差があるため、開始直後のシグナルには誤差が含まれ得る。",
        "3. 約定は板を数量分歩いた VWAP + 固定スリッページで近似しており、実際のマーケット",
        "   インパクトや板の変動（レイテンシ）は再現していない。",
        "4. GitHub Actions の cron は遅延・欠落するため、足の確定直後にシグナルを評価できない",
        "   ことがある（本レポートの「スケジュール実行漏れ」を参照）。",
        "5. サンプル期間が 7 日・各戦略 1 パラメータセットのみであり、統計的有意性はない。",
        "   パラメータ感度分析や複数期間でのウォークフォワード検証が望ましい。",
        "",
    ]
    return "\n".join(lines)


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
