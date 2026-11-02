"""GitHub Pages 用の静的ダッシュボードデータ（docs/data.json）を生成する。

docs/index.html は静的ファイルで、この data.json を fetch して描画する。
APIキー・接続文字列・認証情報は一切含めない（そもそも保持していない）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .metrics import summarize

MAX_POINTS = 1200


def build(root: Path | str, state: dict[str, Any], cfg: dict) -> Path:
    root = Path(root)
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / ".nojekyll").write_text("", encoding="utf-8")

    summary = summarize(state, cfg)
    hist = state.get("equity_history", [])[-MAX_POINTS:]
    payload = {
        "disclaimer": "これは仮想売買（ペーパートレード）の記録です。実際の注文・入出金は一切行っていません。",
        **summary,
        "series": {
            "ts": [h["jst"] for h in hist],
            "price": [float(h["price"]) for h in hist],
            "equity": {
                sid: [float(h["equity"].get(sid, 0)) for h in hist]
                for sid in state.get("portfolios", {})
            },
        },
        "trades": state.get("trades", [])[-500:],
        "strategy_params": _safe_params(cfg),
    }
    out = docs / "data.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
    return out


def _safe_params(cfg: dict) -> dict[str, Any]:
    """公開して問題ない設定のみを抜き出す。"""
    return {
        "fees": {k: str(v) for k, v in cfg.get("fees", {}).items()},
        "risk": {k: str(v) for k, v in cfg.get("risk", {}).items()},
        "strategies": {
            sid: {k: str(v) for k, v in params.items()}
            for sid, params in cfg.get("strategies", {}).items()
        },
    }
