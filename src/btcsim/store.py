"""状態の永続化。

採用方式: **Git リポジトリ自体を正本データストアにする**（JSON + CSV を commit）。
  長所: 完全無料 / 外部サービスの認証情報ゼロ / 誰でも raw URL から読める /
        git commit が原子的で履歴＝完全な監査ログ / 消失リスクが極小。
  短所: 秒単位の高頻度書き込みには不向き（本実験は 10 分間隔なので問題なし）。
比較した候補（README 「永続化方式の比較」に詳述）:
  Supabase 無料 PostgreSQL / Gist / Actions Cache / Artifact。
  Artifact は保持期限があり正本にしない。Cache は追い出されるので使わない。

整合性の担保:
  * state.json は一時ファイル + os.replace で原子的に差し替える
  * transaction() 内で例外が起きたら一切書き込まない（部分更新なし）
  * processed_tick_ids によりリトライ時の二重売買を防止
  * experiment ブロックは初回のみ生成し、以後上書きしない（資金の再設定防止）
"""
from __future__ import annotations

import copy
import csv
import json
import os
import subprocess
import time
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

SCHEMA_VERSION = 1


class StoreError(RuntimeError):
    """状態の読み書きに失敗。発生時は売買を中止する。"""


class GitSyncError(StoreError):
    """ローカルへの保存は成功したが、リモートへの反映に失敗した状態。

    GitHub Actions は毎回クリーンな環境なので、push できないと次回実行で
    状態を復元できない。したがってジョブは失敗として扱う（要調査）。
    """


class Store:
    def __init__(self, root: Path | str):
        self.root = Path(root)
        self.state_dir = self.root / "state"
        self.state_path = self.state_dir / "state.json"
        self.trades_csv = self.state_dir / "trades.csv"
        self.market_csv = self.state_dir / "market.csv"
        self.state: dict[str, Any] = {}

    # --- 読み込み ---
    def load(self) -> dict[str, Any]:
        if not self.state_path.exists():
            self.state = self._empty_state()
            return self.state
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise StoreError(f"state.json を読めません: {exc}") from exc
        if self.state.get("schema") != SCHEMA_VERSION:
            raise StoreError(f"未知のスキーマ: {self.state.get('schema')}")
        return self.state

    @staticmethod
    def _empty_state() -> dict[str, Any]:
        return {
            "schema": SCHEMA_VERSION,
            "experiment": None,            # 初回実行時のみ生成
            "portfolios": {},
            "bars": {},
            "ticks": [],
            "equity_history": [],
            "processed_tick_ids": [],
            "trades": [],
            "stats": {
                "runs": 0, "api_errors": 0, "data_rejections": 0,
                "skipped_duplicate_ticks": 0, "missed_schedules": 0,
                "expected_ticks": 0, "observed_ticks": 0,
            },
            "last_run": None,
        }

    # --- 書き込み ---
    @contextmanager
    def transaction(self) -> Iterator[dict[str, Any]]:
        """成功時のみ原子的に保存する。例外時は何も書かない。"""
        snapshot = copy.deepcopy(self.state)
        try:
            yield self.state
        except BaseException:
            self.state = snapshot          # ロールバック（ディスクは未変更）
            raise
        self._atomic_write()

    def _atomic_write(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        payload = json.dumps(self.state, ensure_ascii=False, indent=1, sort_keys=False)
        try:
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, self.state_path)
        except OSError as exc:
            raise StoreError(f"state.json を書けません: {exc}") from exc

    # --- 派生ファイル（人間/外部ツール向け。正本は state.json） ---
    def export_csv(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        trades = self.state.get("trades", [])
        if trades:
            with self.trades_csv.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(trades[0].keys()))
                w.writeheader()
                w.writerows(trades)
        ticks = self.state.get("ticks", [])
        if ticks:
            with self.market_csv.open("w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=list(ticks[0].keys()))
                w.writeheader()
                w.writerows(ticks)

    # --- 冪等性 ---
    def is_tick_processed(self, tick_id: str) -> bool:
        return tick_id in self.state.get("processed_tick_ids", [])

    def mark_tick_processed(self, tick_id: str, keep: int = 2000) -> None:
        ids = self.state.setdefault("processed_tick_ids", [])
        if tick_id not in ids:
            ids.append(tick_id)
        del ids[:-keep]

    # --- git 同期 ---
    def git_sync(self, message: str, paths: list[str] | None = None, attempts: int = 3) -> bool:
        """変更を commit して push する。競合時は rebase して再試行。"""
        paths = paths or ["state", "docs", "reports"]
        paths = [p for p in paths if (self.root / p).exists()]
        if not paths:
            return True
        env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}

        def run(*args: str, check: bool = True) -> subprocess.CompletedProcess:
            return subprocess.run(["git", *args], cwd=self.root, env=env, check=check,
                                  capture_output=True, text=True)

        try:
            run("config", "user.name", os.environ.get("GIT_AUTHOR_NAME", "btc-sim-bot"))
            run("config", "user.email", os.environ.get("GIT_AUTHOR_EMAIL", "btc-sim-bot@users.noreply.github.com"))
            run("add", "--", *paths)
            if not run("diff", "--cached", "--quiet", check=False).returncode:
                return True  # 変更なし
            run("commit", "-m", message)
        except subprocess.CalledProcessError as exc:
            raise GitSyncError(f"git commit 失敗: {exc.stderr}") from exc

        for i in range(attempts):
            if run("push", check=False).returncode == 0:
                return True
            run("pull", "--rebase", "--autostash", check=False)
            time.sleep(2 * (i + 1))
        raise GitSyncError(f"git push に {attempts} 回失敗しました（状態はローカルに保存済み）")


def dec(value: Any) -> Decimal:
    return Decimal(str(value))
