"""FX 実験のダッシュボード生成。

- `docs/fx-data.json`  … Web サーバー / GitHub Pages 用（docs/fx.html が fetch する）
- `docs/fx-report.html` … データ埋め込み済みの単体 HTML（file:// でそのまま開ける）

現物のダッシュボード（docs/index.html, docs/data.json）には一切触れない。
認証情報は含めない（そもそも保持していない）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..config import ROOT as CONFIG_ROOT
from ..dashboard import MARKER_END, MARKER_START, build_standalone
from ..store import Store
from .metrics import spot_comparison, summarize

MAX_POINTS = 1200


def build(root: Path | str, state: dict[str, Any], cfg: dict) -> Path:
    root = Path(root)
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)

    summary = summarize(state, cfg)
    hist = state.get("equity_history", [])[-MAX_POINTS:]
    payload = {
        "disclaimer": "これは仮想売買（ペーパートレード）の記録です。実際の注文・入出金は一切行っていません。"
                      "証拠金取引（ショート可・レバレッジあり）のシミュレーションです。",
        **summary,
        "series": {
            "ts": [h["jst"] for h in hist],
            "price": [float(h["price"]) for h in hist],
            "equity": {sid: [float(h["equity"].get(sid, 0)) for h in hist]
                       for sid in state.get("portfolios", {})},
        },
        "trades": state.get("trades", [])[-500:],
        "spot_comparison": _spot_comparison(root, summary),
        "strategy_params": _safe_params(cfg),
    }
    out = docs / "fx-data.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    template = docs / "fx.html"
    if not template.exists():
        template = CONFIG_ROOT / "docs" / "fx.html"
    if template.exists():
        html = template.read_text(encoding="utf-8")
        if MARKER_START in html and MARKER_END in html:
            head, rest = html.split(MARKER_START, 1)
            _, tail = rest.split(MARKER_END, 1)
            embedded = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
            (docs / "fx-report.html").write_text(
                head + MARKER_START + " " + embedded + " " + MARKER_END + tail, encoding="utf-8")
    return out


def _spot_comparison(root: Path, summary: dict[str, Any]) -> dict[str, Any]:
    """現物実験の state を読んで比較を作る。読めなければ比較なしで続行する。"""
    try:
        from ..config import Config
        spot_store = Store(root, "state")
        spot_state = spot_store.load()
        if not spot_state.get("experiment"):
            return {"available": False}
        return spot_comparison(summary, spot_state, Config.load())
    except Exception:                       # noqa: BLE001 - 比較は無くても FX 実験は続ける
        return {"available": False}


def _safe_params(cfg: dict) -> dict[str, Any]:
    return {
        "fees": {k: str(v) for k, v in cfg.get("fees", {}).items()},
        "risk": {k: str(v) for k, v in cfg.get("risk", {}).items()},
        "strategies": {sid: {k: str(v) for k, v in params.items()}
                       for sid, params in cfg.get("strategies", {}).items()},
    }
