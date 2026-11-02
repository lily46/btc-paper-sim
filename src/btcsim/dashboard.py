"""ダッシュボードの生成。

- `docs/data.json`  … Web サーバー / GitHub Pages 用（docs/index.html が fetch する）
- `docs/report.html` … データ埋め込み済みの単体 HTML（file:// でそのまま開ける）
APIキー・接続文字列・認証情報は一切含めない（そもそも保持していない）。
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config import ROOT as CONFIG_ROOT
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
    build_standalone(docs, payload)
    return out


MARKER_START = "/*__EMBEDDED_DATA__*/"
MARKER_END = "/*__END_EMBEDDED_DATA__*/"


def build_standalone(docs: Path, payload: dict[str, Any], out: Path | None = None) -> Path | None:
    """docs/report.html（データ埋め込み済みの単体HTML）を生成する。

    file:// で開いた HTML は fetch("data.json") をブラウザにブロックされるため、
    ローカルで `git pull` して直接ダブルクリックで見るにはこの形式が必要。
    テンプレートは docs/index.html を再利用し、UI の定義を一箇所に保つ。
    """
    template = docs / "index.html"
    if not template.exists():                      # --root を別ディレクトリにした場合
        template = CONFIG_ROOT / "docs" / "index.html"
    if not template.exists():
        return None
    html = template.read_text(encoding="utf-8")
    if MARKER_START not in html or MARKER_END not in html:
        return None
    head, rest = html.split(MARKER_START, 1)
    _, tail = rest.split(MARKER_END, 1)
    # </script> がデータ中に現れても壊れないようエスケープする
    embedded = json.dumps(payload, ensure_ascii=False, default=str).replace("</", "<\\/")
    out = out or (docs / "report.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(head + MARKER_START + " " + embedded + " " + MARKER_END + tail, encoding="utf-8")
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
