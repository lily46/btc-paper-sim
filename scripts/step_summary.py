"""GitHub Actions のジョブサマリー用に現在状況を Markdown で出力する。"""
from __future__ import annotations

import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

from btcsim.config import Config          # noqa: E402
from btcsim.metrics import summarize      # noqa: E402

path = pathlib.Path("state/state.json")
if not path.exists():
    print("- state.json がまだありません（初回実行前）")
    raise SystemExit(0)

state = json.loads(path.read_text(encoding="utf-8"))
s = summarize(state, Config.load())
exp, mkt = s["experiment"], s["market"]
print("> 仮想売買（ペーパートレード）です。実注文は行っていません。")
print("")
print(f"- status: `{exp['status']}` / 開始 {exp['started_at_jst']} / 終了予定 {exp['ends_at_jst']} / 残り {exp['remaining_human']}")
print(f"- 最終取得 (JST): {mkt['last_fetch_jst']} / LTP: {mkt['ltp']} 円 / 開始比 {mkt['price_change_pct']} %")
last = state.get("last_run") or {}
print(f"- last_run: `{last.get('status')}` tick={last.get('tick_id')} trades={last.get('trades', 0)}")
print("")
print("| 戦略 | 総資産(円) | 損益(円) | JPY残高 | BTC | 売買回数 | 手数料 | 最大DD% |")
print("|---|---:|---:|---:|---:|---:|---:|---:|")
for sid in sorted(s["strategies"]):
    r = s["strategies"][sid]
    print(f"| {sid} | {r['equity_jpy']} | {r['total_pnl_jpy']} | {r['jpy_balance']} | "
          f"{r['btc_balance']} | {r['trades']} | {r['fees_jpy']} | {r['max_drawdown_pct']} |")
for sid in sorted(s["strategies"]):
    d = ((last.get("decisions") or {}).get(sid) or {})
    if d:
        print(f"- {sid}: {d.get('action')} executed={d.get('executed')}: {d.get('reason')}")
