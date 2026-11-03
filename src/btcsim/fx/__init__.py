"""FX（bitFlyer Lightning FX / 証拠金取引）の仮想売買モデル。

現物の実験（state/）とは完全に独立して動く:
  * 状態は state_fx/state.json
  * ダッシュボードは docs/fx.html + docs/fx-data.json
  * レポートは reports/fx_final_report.*
  * Workflow は .github/workflows/simulate-fx.yml（別 concurrency group）

ショート（空売り）を扱うが、**実注文は一切行わない**。
bitFlyer の Public API（認証不要）で FX_BTC_JPY の価格と板を読むだけ。
"""
