"""最終レポートを Markdown / JSON / CSV / HTML で出力する。"""
from __future__ import annotations

import csv
import json
import shutil
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import dashboard
from .metrics import summarize

COLUMNS = [
    "strategy", "started_at_jst", "ends_at_jst", "window_status",
    "equity_jpy", "total_pnl_jpy", "total_pnl_pct", "jpy_balance",
    "btc_balance", "realized_pnl_jpy", "unrealized_pnl_jpy", "trades", "buys",
    "sells", "closed_trades", "win_rate_pct", "avg_win_jpy", "avg_loss_jpy",
    "breakeven_win_rate_pct", "avg_hold_minutes", "fees_jpy", "max_drawdown_pct",
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

    # HTML 版（データ埋め込み済みの単体ファイル。file:// で直接開ける固定スナップショット）
    dashboard.build(root, state, cfg)
    html_src = root / "docs" / "report.html"
    html_path = out_dir / "final_report.html"
    if html_src.exists():
        shutil.copyfile(html_src, html_path)
    else:
        html_path = None

    return {"json": json_path, "csv": csv_path, "md": md_path, "html": html_path}


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

    lines += ["", "## 戦略ごとの実験期間", "",
              "戦略 E は途中から追加されたため、A〜D とは独立した 168 時間を持つ。",
              "", "| 戦略 | 開始 (JST) | 終了予定 (JST) | 状態 | 独自期間 |",
              "|---|---|---|---|---|"]
    for sid in sorted(rows):
        r = rows[sid]
        lines.append(f"| {sid} | {r.get('started_at_jst')} | {r.get('ends_at_jst')} | "
                     f"{r.get('window_status')} | {'はい' if r.get('own_window') else 'いいえ（A〜D と共通）'} |")

    lines += _common_period_section(s)
    lines += _strategy_e_section(s, state)

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


def _common_period_section(s: dict[str, Any]) -> list[str]:
    """開始日が違う戦略を公平に比べるための共通期間セクション。"""
    cp = s.get("common_period") or {}
    if not cp.get("available"):
        return ["", "## 共通期間での比較", "", "（共通期間のデータがまだ足りません）"]
    lines = [
        "", "## 共通期間での比較", "",
        f"全戦略が稼働している期間のみで比較する（最も遅く開始した戦略の開始時刻以降）。",
        f"- 対象期間: {cp['from_jst']} 〜 {cp['to_jst']}（{cp['hours']} 時間）",
        f"- 同期間の BTC 騰落率: {cp.get('btc_change_pct')} %",
        "",
        "| 戦略 | 期間開始時の総資産 | 現在の総資産 | 期間損益(円) | 期間損益率(%) | B&H差(円) |",
        "|---|---:|---:|---:|---:|---:|",
    ]
    for sid in sorted(cp["strategies"]):
        r = cp["strategies"][sid]
        lines.append(f"| {sid} | {r['start_equity_jpy']} | {r['end_equity_jpy']} | "
                     f"{r['pnl_jpy']} | {r['pnl_pct']} | {r.get('vs_buy_and_hold_jpy', '-')} |")
    return lines


def _strategy_e_section(s: dict[str, Any], state: dict[str, Any]) -> list[str]:
    """戦略 E（短期モメンタム）の売買分析。"""
    sid = "E_short_momentum"
    r = (s.get("strategies") or {}).get(sid)
    if not r:
        return []
    trades = [t for t in state.get("trades", []) if t.get("strategy") == sid]
    sells = [t for t in trades if t.get("side") == "SELL"]
    win = (s.get("strategy_windows") or {}).get(sid) or {}
    started = int(win.get("started_ts") or 0)
    hist = state.get("equity_history") or []
    now_ts = int(hist[-1]["ts"]) if hist else started
    days = max((now_ts - started) / 86400, 1 / 24) if started else 1
    fees = Decimal(r["fees_jpy"])
    pnl = Decimal(r["total_pnl_jpy"])
    kinds = r.get("exit_breakdown") or {}
    total_exits = sum(kinds.values())

    lines = [
        "", "## 戦略 E（短期モメンタム）の売買分析", "",
        f"- 稼働期間: {win.get('started_at_jst')} 〜 {win.get('ends_at_jst')}（{days:.2f} 日経過）",
        f"- 売買回数: {r['trades']} 回（買い {r['buys']} / 売り {r['sells']}）",
        f"- **1 日あたり平均 {Decimal(r['trades']) / Decimal(str(round(days, 4))):.2f} 回**",
        f"- 平均保有時間: {r.get('avg_hold_minutes') or '-'} 分",
        f"- 勝率: {r.get('win_rate_pct') or '-'} %（決済 {r['closed_trades']} 件）",
        f"- 平均利益: {r.get('avg_win_jpy') or '-'} 円 / 平均損失: {r.get('avg_loss_jpy') or '-'} 円",
        f"- **損益分岐勝率: {r.get('breakeven_win_rate_pct') or '-'} %**"
        "（平均損失 ÷ (平均利益 + 平均損失)。これを上回らないと手数料込みで勝てない）",
        f"- 累計手数料: {fees} 円",
        f"- 損益: {pnl} 円（{r['total_pnl_pct']} %）",
    ]
    if fees > 0:
        lines.append(f"- **手数料を払わなかった場合の損益: {pnl + fees} 円** "
                     f"→ 手数料が利益を {fees} 円（{abs(fees / Decimal(r['equity_jpy']) * 100):.3f}%）削った")
    if total_exits:
        lines += ["", "### エグジット理由の内訳", "", "| 理由 | 回数 | 割合 |", "|---|---:|---:|"]
        label = {"take_profit": "利確 (+0.8%)", "stop_loss": "損切り (-0.5%)",
                 "timeout": "最大保有時間 (6h)", "trend_reversal": "トレンド反転",
                 "signal": "その他シグナル"}
        for kind, n in sorted(kinds.items(), key=lambda kv: -kv[1]):
            lines.append(f"| {label.get(kind, kind)} | {n} | {n / total_exits * 100:.1f}% |")
    else:
        lines += ["", "（まだ決済が発生していないため、エグジット内訳はありません）"]

    if sells:
        lines += ["", "### 決済トレード一覧", "",
                  "| 売却日時 | 建値 | 売値 | 数量 | 保有(分) | 手数料 | 実現損益 | エグジット理由 |",
                  "|---|---:|---:|---:|---:|---:|---:|---|"]
        for t in sells:
            lines.append(f"| {t['ts']} | {t.get('entry_price') or '-'} | {t['price']} | {t['size']} | "
                         f"{t.get('hold_minutes') or '-'} | {t['fee_jpy']} | {t['realized_pnl']} | "
                         f"{t.get('exit_kind') or '-'} |")

    others = {k: v for k, v in (s.get("strategies") or {}).items() if k != sid}
    if others:
        lines += ["", "### A〜D との比較（全期間ベース）", "",
                  "| 戦略 | 総資産(円) | 損益(円) | 売買回数 | 手数料(円) | 最大DD(%) |",
                  "|---|---:|---:|---:|---:|---:|"]
        for k in sorted(others):
            o = others[k]
            lines.append(f"| {k} | {o['equity_jpy']} | {o['total_pnl_jpy']} | {o['trades']} | "
                         f"{o['fees_jpy']} | {o['max_drawdown_pct']} |")
        lines.append(f"| **{sid}** | **{r['equity_jpy']}** | **{r['total_pnl_jpy']}** | "
                     f"**{r['trades']}** | **{r['fees_jpy']}** | **{r['max_drawdown_pct']}** |")
        lines.append("")
        lines.append("※ E は開始日が異なるため、公平な比較は上の「共通期間での比較」を参照。")
    return lines


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
