"""FX 実験の最終レポート（Markdown / JSON / CSV / HTML）。"""
from __future__ import annotations

import csv
import json
import shutil
from decimal import Decimal
from pathlib import Path
from typing import Any

from . import dashboard as fx_dashboard
from .metrics import summarize

COLUMNS = [
    "strategy", "equity_jpy", "total_pnl_jpy", "total_pnl_pct", "collateral_jpy",
    "realized_pnl_jpy", "unrealized_pnl_jpy", "trades", "opens", "closes",
    "longs", "shorts", "liquidations", "closed_trades", "win_rate_pct",
    "avg_win_jpy", "avg_loss_jpy", "breakeven_win_rate_pct", "avg_hold_minutes",
    "fees_jpy", "swap_jpy", "total_cost_jpy", "max_drawdown_pct", "halted",
]


def build(root: Path | str, state: dict[str, Any], cfg: dict) -> dict[str, Path | None]:
    root = Path(root)
    out_dir = root / "reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = summarize(state, cfg)

    json_path = out_dir / "fx_final_report.json"
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=1, default=str),
                         encoding="utf-8")

    csv_path = out_dir / "fx_final_report.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for row in summary["strategies"].values():
            w.writerow({c: row.get(c) for c in COLUMNS})

    md_path = out_dir / "fx_final_report.md"
    md_path.write_text(_markdown(summary, state, root), encoding="utf-8")

    fx_dashboard.build(root, state, cfg)
    html_src = root / "docs" / "fx-report.html"
    html_path: Path | None = out_dir / "fx_final_report.html"
    if html_src.exists():
        shutil.copyfile(html_src, html_path)
    else:
        html_path = None
    return {"json": json_path, "csv": csv_path, "md": md_path, "html": html_path}


def _markdown(s: dict[str, Any], state: dict[str, Any], root: Path) -> str:
    exp, mkt, stats = s["experiment"], s["market_data"], s.get("stats", {})
    rows = s["strategies"]
    trades = state.get("trades", [])
    lines = [
        "# BTC FX（証拠金取引）仮想売買シミュレーター 最終レポート",
        "",
        "> **これは仮想売買（ペーパートレード）の記録です。実際の注文・入出金は一切行っていません。**",
        "> ショート（空売り）とレバレッジを含む証拠金取引のシミュレーションです。",
        "",
        f"- 生成時刻 (UTC): {s['generated_at_utc']}",
        f"- 実験開始 (JST): {exp['started_at_jst']} / 終了予定: {exp['ends_at_jst']}",
        f"- 実験終了 (JST): {exp.get('finished_at_jst') or '（未終了）'} / ステータス: {exp['status']}",
        f"- 銘柄: {exp['product_code']}（bitFlyer Lightning FX）",
        f"- レバレッジ: **{exp['leverage']} 倍** / ロスカット: 証拠金維持率 {exp['liquidation_margin_ratio_pct']}% 未満",
        f"- 建玉管理料: {exp['swap_rate_per_day_pct']} %/日 / 取引手数料: 片道 {s.get('strategy_params', {}).get('fees', {}).get('taker_fee_rate', '0')}",
        f"- 初期証拠金: {exp['initial_capital_jpy']} 円 × 各戦略",
        "",
        "## FX 価格",
        "",
        f"- 開始価格: {mkt['start_price']} 円 / 最終価格: {mkt['ltp']} 円 / 騰落率: {mkt['price_change_pct']} %",
        f"- データ取得元: {mkt['source']}",
        f"- 最終取得時刻 (JST): {mkt['last_fetch_jst']}",
        "",
        "## 戦略別成績",
        "",
        "| 戦略 | 最終有効証拠金(円) | 損益(円) | 損益率(%) | 建玉 | 売買回数 | 買/売 | ロスカット | 勝率(%) | 手数料 | 建玉管理料 | 最大DD(%) |",
        "|---|---:|---:|---:|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for sid in sorted(rows):
        r = rows[sid]
        pos = f"{r['position_side']} {r['position_size_btc']}" if r["position_side"] else "なし"
        lines.append(
            f"| {sid} | {r['equity_jpy']} | {r['total_pnl_jpy']} | {r['total_pnl_pct']} | {pos} | "
            f"{r['trades']} | {r['longs']}/{r['shorts']} | {r['liquidations']} | "
            f"{r['win_rate_pct'] or '-'} | {r['fees_jpy']} | {r['swap_jpy']} | {r['max_drawdown_pct']} |")

    total_fees = sum((Decimal(r["fees_jpy"]) for r in rows.values()), Decimal(0))
    total_swap = sum((Decimal(r["swap_jpy"]) for r in rows.values()), Decimal(0))
    lines += [
        "",
        f"- 全戦略の手数料総額: {total_fees} 円 / 建玉管理料総額: {total_swap} 円",
        "",
        "## 勝率の算出方法",
        "",
        "決済 1 回を 1 トレードとし、手数料込みの実現損益がプラスだった割合",
        "（wins / (wins + losses)）。**強制ロスカットも決済 1 回として数える**。",
        "損益分岐勝率 = 平均損失 ÷ (平均利益 + 平均損失)。",
        "",
        "## ロング / ショート別の成績",
        "",
        "| 戦略 | 方向 | 決済数 | 勝率(%) | 損益(円) |",
        "|---|---|---:|---:|---:|",
    ]
    for sid in sorted(rows):
        for side, v in sorted((rows[sid].get("by_side") or {}).items()):
            lines.append(f"| {sid} | {side} | {v['closes']} | {v['win_rate_pct'] or '-'} | {v['pnl_jpy']} |")
    if not any(rows[sid].get("by_side") for sid in rows):
        lines.append("| （まだ決済がありません） | - | - | - | - |")

    lines += ["", "## エグジット理由の内訳", ""]
    label = {"take_profit": "利確", "stop_loss": "損切り", "timeout": "時間切れ",
             "reversal": "ドテン（反転）", "liquidation": "**強制ロスカット**", "signal": "シグナル"}
    for sid in sorted(rows):
        kinds = rows[sid].get("exit_breakdown") or {}
        if not kinds:
            continue
        total = sum(kinds.values())
        parts = " / ".join(f"{label.get(k, k)} {n}回({n / total * 100:.0f}%)"
                           for k, n in sorted(kinds.items(), key=lambda kv: -kv[1]))
        lines.append(f"- **{sid}**: {parts}")
    if not any(rows[sid].get("exit_breakdown") for sid in rows):
        lines.append("- （まだ決済がありません）")

    lines += _spot_section(s)

    lines += [
        "",
        "## 実行品質",
        "",
        f"- Workflow 実行回数: {stats.get('runs', 0)}",
        f"- 期待ティック数: {stats.get('expected_ticks', 0)} / 処理したティック数: {stats.get('observed_ticks', 0)}",
        f"- スケジュール実行漏れ: {stats.get('missed_schedules', 0)} 回",
        f"- API エラー回数: {stats.get('api_errors', 0)} 回",
        f"- 異常値検出による売買スキップ: {stats.get('data_rejections', 0)} 回",
        f"- 重複ティックのスキップ（二重発注防止）: {stats.get('skipped_duplicate_ticks', 0)} 回",
        f"- 記録された建玉操作: {len(trades)} 件",
        "",
        "## 改善すべき点 / 既知の限界",
        "",
        "1. 10 分ごとの離散監視なので、利確・損切り・**ロスカットのラインを一時的に割って",
        "   戻した動き**は捉えられない。実際の取引所は秒単位でロスカットするため、",
        "   本シミュレーションはロスカットを過小評価している可能性がある。",
        "2. 建玉管理料は 1 日 1 回の課金を経過時間で按分している（合計額は同じだが、",
        "   実際の課金タイミングとはずれる）。料率も設定値であり、最新の公式料率は要確認。",
        "3. FX は現物と価格が乖離する（プレミアム/ディスカウント）。現物実験との比較は",
        "   「同じ値動きでの比較」ではなく「別市場での比較」である点に注意。",
        "4. 必要証拠金は現在価格ベースで計算している。取引所の実際の計算方法",
        "   （建玉評価額の扱いや両建て時の扱い）とは差異がありうる。",
        "5. 期間 7 日・1 パラメータセットのみであり、統計的有意性はない。",
        "   とくにショートは下落局面に偏った期間では有利に見えるため、",
        "   複数期間での検証が必要。",
        "",
    ]
    return "\n".join(lines)


def _spot_section(s: dict[str, Any]) -> list[str]:
    cmp_ = s.get("spot_comparison") or {}
    lines = ["", "## 現物実験との比較（空売り解禁の効果）", ""]
    if not cmp_.get("available"):
        return lines + ["（現物実験のデータが読めないため比較なし）"]
    lines += [
        f"FX は現物実験より後に開始したため、現物側は **{cmp_.get('since_jst')} 以降**の損益で比較する。",
        "",
        "| FX 戦略 | 対応する現物戦略 | FX 損益(円) | 現物 損益(同期間・円) | 差（空売り解禁の効果） | FX 売買回数 | うちショート |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for sid, r in sorted(cmp_["pairs"].items()):
        lines.append(
            f"| {sid} | {r['spot_strategy']} | {r['fx_pnl_jpy']} | "
            f"{r.get('spot_pnl_since_fx_start_jpy', '-')} | {r.get('short_effect_jpy', '-')} | "
            f"{r['fx_trades']} | {r['fx_shorts']} |")
    lines += [
        "",
        "※「差」は FX 側の損益から同期間の現物側の損益を引いたもの。プラスなら",
        "  「ショートとレバレッジを使えたぶん有利だった」ことを意味するが、",
        "  レバレッジ 2 倍の影響と空売りの影響が混ざっている点に注意。",
    ]
    return lines
