"""GitHub Actions のジョブサマリー用（FX 実験）。"""
from __future__ import annotations

import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from btcsim.config import Config, FX_CONFIG_PATH      # noqa: E402
from btcsim.fx.metrics import summarize               # noqa: E402

path = pathlib.Path("state_fx/state.json")
if not path.exists():
    print("- state_fx/state.json がまだありません（初回実行前）")
    raise SystemExit(0)

state = json.loads(path.read_text(encoding="utf-8"))
s = summarize(state, Config.load(FX_CONFIG_PATH))
exp, mkt = s["experiment"], s["market_data"]
print("> 仮想売買（ペーパートレード）です。ショート・レバレッジを含みますが実注文は行っていません。")
print("")
print(f"- status: `{exp['status']}` / 開始 {exp['started_at_jst']} / 終了予定 {exp['ends_at_jst']} / 残り {exp['remaining_human']}")
print(f"- レバレッジ {exp['leverage']}倍 / ロスカット維持率 {exp['liquidation_margin_ratio_pct']}% / 建玉管理料 {exp['swap_rate_per_day_pct']}%/日")
print(f"- 最終取得 (JST): {mkt['last_fetch_jst']} / LTP: {mkt['ltp']} 円 / 開始比 {mkt['price_change_pct']} %")
last = state.get("last_run") or {}
print(f"- last_run: `{last.get('status')}` tick={last.get('tick_id')} trades={last.get('trades', 0)}")
print("")
print("| 戦略 | 有効証拠金 | 損益 | 建玉 | 維持率% | 回数(買/売) | ロスカット | 手数料+管理料 | 最大DD% |")
print("|---|---:|---:|---|---:|---:|---:|---:|---:|")
for sid in sorted(s["strategies"]):
    r = s["strategies"][sid]
    pos = f"{r['position_side']} {r['position_size_btc']}" if r["position_side"] else "なし"
    print(f"| {sid} | {r['equity_jpy']} | {r['total_pnl_jpy']} | {pos} | "
          f"{r['margin_ratio_pct'] or '-'} | {r['trades']}({r['longs']}/{r['shorts']}) | "
          f"{r['liquidations']} | {r['total_cost_jpy']} | {r['max_drawdown_pct']} |")
for sid in sorted(s["strategies"]):
    d = ((last.get("decisions") or {}).get(sid) or {})
    if d:
        print(f"- {sid}: {d.get('action')} {d.get('side') or ''} executed={d.get('executed')}: {d.get('reason')}")
