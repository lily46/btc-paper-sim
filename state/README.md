# state/ — 実験状態の正本

- `state.json` : 正本データ（残高・BTC 保有量・足・売買履歴・統計）。
  GitHub Actions が 10 分ごとに原子的に更新し commit します。**手で編集しないでください。**
- `trades.csv` / `market.csv` : `state.json` から生成される派生ファイル（閲覧用）。

`state.json` を削除して commit すると、次回実行で**新しい実験**が初期化されます。
