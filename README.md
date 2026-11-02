# Bitcoin 自動売買シミュレーター（bitFlyer 実データ / 仮想売買専用）

> ⚠️ **これは仮想売買（ペーパートレード）のみのプロジェクトです。**
> 実際の注文・入出金・取引所口座への接続は一切行いません。
> bitFlyer の **Public API（認証不要・読み取り専用）** しか呼びません。
> APIキー・シークレットは要求も保存もしません（Private API を叩くコードが
> 存在しないことは `tests/test_store_and_run.py::test_no_private_api_endpoints_in_source`
> で機械的に検査しています）。

仮想資金 **各戦略 100 万円 × 4 戦略** を **168 時間（7 日間）** 運用し、同一の市場データで
成績を比較します。GitHub Actions が 10 分ごとに自動実行するため、PC の電源は不要です。

---

## 1. 全体像

```
GitHub Actions (cron 0,10,20,30,40,50 * * * *)
  └─ python3 -m btcsim.run --git --loop-minutes 25   ← 1 起動が 25 分常駐し 10 分境界ごとに処理
       ├─ bitFlyer Public API から価格 / 板 / 約定履歴を取得
       ├─ 30分足・1時間足を更新（確定足のみ指標に使用＝ルックアヘッド禁止）
       ├─ 4 戦略のシグナル判定 → 仮想売買（Decimal 計算）
       ├─ state/state.json を原子的に更新 → git commit & push（= 正本データ）
       └─ docs/data.json と docs/report.html（単体HTML）を再生成
```

| ファイル | 役割 |
|---|---|
| `src/btcsim/api.py` | 市場データ取得（Public API のみ） |
| `src/btcsim/candles.py` | ローソク足の生成・確定足の抽出 |
| `src/btcsim/indicators.py` | SMA / RSI（Wilder） |
| `src/btcsim/portfolio.py` | 仮想残高・板を歩く約定シミュレーション・資金管理ルール |
| `src/btcsim/strategies.py` | 戦略 A / B / C / D |
| `src/btcsim/store.py` | 永続化（JSON + CSV を git にコミット）・冪等性 |
| `src/btcsim/run.py` | 1 ティックのオーケストレーション（エントリポイント） |
| `src/btcsim/metrics.py` | 集計（ダッシュボードとレポートで共通） |
| `src/btcsim/dashboard.py` | `docs/data.json` と `docs/report.html`（単体HTML）生成 |
| `src/btcsim/report.py` | 最終レポート（Markdown / JSON / CSV / HTML） |
| `config.json` | すべてのパラメータ（戦略・手数料・リスク管理・異常値判定） |
| `docs/index.html` | ダッシュボード UI（テンプレート兼 Web サーバー用） |
| `docs/report.html` | **自動生成**：データ埋め込み済みの単体 HTML（ローカル閲覧用） |

- Python **3.12 以降**。**実行時の依存ライブラリはゼロ**（標準ライブラリのみ）。
  1 ティックの処理自体は数秒で終わります。

### スケジュールについて（実測にもとづく設計変更）

当初は `*/10 * * * *` で 10 分ごとに 1 ティックずつ処理する設計でしたが、**実測で
1 時間以上スケジュールが発火しませんでした**（GitHub のスケジュールは公式に
「遅延・欠落あり」と明記されており、短間隔ほど取りこぼしやすい）。

そこで **cron は 10 分ごとのまま、各起動を 25 分常駐させて 10 分境界ごとに
ティックを処理する**方式にしました（`--loop-minutes 25`）。
「発火したら 1 ティック処理する」ではなく、**「発火を保険にして常時サンプリングする」**
という考え方です。

- サンプリング間隔は **10 分**（実験条件は当初のまま）
- cron が 1 回も発火しない時間帯があっても、走っているプロセスがティックを拾い続ける
- 逆に毎回発火した場合も、concurrency で直列化され、`processed_tick_ids` により
  **同じティックは二度処理されない**（＝二重売買しない）
- 結果として runner はほぼ常時稼働する。**public リポジトリなので Actions の実行時間は
  無制限・無料**。private に戻す場合は無料枠 2,000 分/月を超えるので、
  `--loop-minutes 0`（1 起動 1 ティック）に戻すこと
- cron 式は `*/10` ではなく `0,10,20,30,40,50` と明示列挙（ステップ構文が
  無視される事例の回避）

---

## 2. 戦略と売買条件（明文化）

4 戦略はすべて同じティック・同じ板・同じ手数料・同じ資金管理ルールで評価されます。
判定に使うのは **確定した足のみ**（進行中の足は使いません）。

| | 戦略 | 時間足 | 買い条件 | 売り条件 |
|---|---|---|---|---|
| A | 移動平均クロス（順張り） | 30分足 | 短期 SMA(6) が長期 SMA(24) を**上抜け**た足で買い | 短期が長期を**下抜け**た足で全量売り |
| B | RSI 逆張り | 30分足 | RSI(14) が 30 **以下に入った**足で買い | RSI(14) が 70 **以上に入った**足で全量売り |
| C | 複合 | 1時間足 + 30分足 | 1h 終値 > 1h SMA(24)（上昇トレンド）**かつ** 30m RSI が 40 以下に入った足 | トレンドが下向きに転換、または 30m RSI が 60 以上に入った（保有時のみ） |
| D | Buy & Hold（比較用） | — | 実験開始の最初のティックで全額購入（1 回のみ） | **売らない** |

共通ルール:

- **買いは `best_ask` 側、売りは `best_bid` 側** の板を注文数量ぶん歩いた VWAP で約定。
  さらに `slippage_bps`（既定 5bp = 0.05%）を不利方向に加算。
- 板の流動性が足りない場合は、板に存在する数量までしか約定しません。
- 手数料は片道 `taker_fee_rate`（既定 **0.15%**）。買いは JPY から、売りは受取 JPY から控除。
- 売りは常に**全量**（部分利確なし）。4 戦略で同じ建玉管理にして比較の公平性を保つため。
- **同一シグナルの重複発注禁止**: シグナルには `戦略:時間足:足の開始時刻:向き` のキーが付き、
  同じキーで 2 回目の発注は抑止されます（`last_signal_key`）。
- B / C は「しきい値をまたいだ足」のみを発注条件にしているため、売られすぎが続く間に
  買いを連打しません。
- 指標が成立するまで（必要本数の確定足が揃うまで）**一切取引しません**。

**D だけの扱い**: 仕様どおり「開始時に 100 万円分購入して 7 日間保有」するベンチマークなので、
D のみ「1 回 20% 上限」「BTC 比率 80% 上限」を適用しません（適用すると Buy & Hold に
ならず比較対象として成立しないため）。A / B / C は両方の上限が常に効きます。
この差分は意図的な設計であり、レポート・ダッシュボードでも D は「比較用」として扱います。

### 資金管理ルール（A / B / C）

- 現物のみ。レバレッジ・借入・空売りは**実装自体が存在しません**（残高・数量が負になる経路がない）。
- 1 回の購入 ≤ 初期資金の **20%**（`max_order_pct_of_initial`）
- BTC 評価額 ≤ 総資産の **80%**（`max_btc_pct_of_equity`）
- 最低注文数量 **0.001 BTC**、数量精度 **8 桁**（切り捨て）
- 残高を超える注文は不可（手数料込みで判定）
- **最大ドローダウン 10% 到達で当該戦略の新規売買を停止**（強制決済はしない）
- API 障害・異常値検出時はそのティックの売買を全戦略スキップ

### 異常値の判定（`config.json` の `sanity`）

価格 ≤ 0 / 想定レンジ外（10万円〜5億円）/ `best_bid >= best_ask` / スプレッド > 1% /
直前ティックからの価格変動 > 15% のいずれかで、そのティックは**データごと破棄**し売買しません。

---

## 3. 市場データと精度（重要）

| 用途 | データ源 | 備考 |
|---|---|---|
| 約定価格・評価 | bitFlyer `/v1/getticker`, `/v1/getboard` | `best_bid` / `best_ask` / 板 30 レベル |
| 足の高値・安値・出来高 | bitFlyer `/v1/getexecutions`（直近約定） | 実行ごとに取得して足へ反映 |
| 起動直後のウォームアップ足 | **bitbank 公開 1 分足**（無認証） | 代理データ。bitFlyer はローソク足 API を公開していないため |

- 評価額は保守的に **`best_bid`**（すぐ売れる価格）で計算します。
- **精度の限界**: 10 分ごとの実行では約定履歴が実行間隔全体をカバーできないことがあり、
  その区間の高値・安値・出来高は完全ではありません（ティック価格で補完）。
  この限界は最終レポートの「改善すべき点」にも明記されます。
- 実験開始直後は、指標（SMA24 / RSI14）を成立させるために bitbank の 1 分足を集約した
  過去足を 1 度だけ投入します（`config.json` の `data.warmup_bootstrap: false` で無効化可）。
  無効にした場合は、自前の足が溜まるまで（30分足 24 本 ≒ 12 時間）A/B/C は取引しません。
- **ルックアヘッドバイアス禁止**: 指標計算には `candles.closed_bars()` を通した確定足のみを
  使用します（テスト `test_no_lookahead_unclosed_bar_ignored` で検証）。
- 実際の取得時刻は `fetched_at_jst` / `fetched_at_utc` として毎ティック記録され、
  cron の遅延・欠落は `missed_schedules` として集計されます。

---

## 4. 永続化方式の比較と採用理由

| 方式 | コスト | 外部からの読み書き | 整合性 | 消失リスク | 判定 |
|---|---|---|---|---|---|
| **Git（本リポジトリに JSON/CSV をコミット）** | 無料 | raw URL / git clone で誰でも読める | commit が原子的、履歴が完全な監査ログ | ほぼ無し（履歴から復元可能） | **採用** |
| Supabase 無料 PostgreSQL | 無料枠あり | 可（要認証） | トランザクション強い | 無料枠は**非アクティブで一時停止**されることがある。Secrets 管理が必要 | 次善 |
| GitHub Actions Cache | 無料 | 不可 | 弱い | 7 日/10GB で**追い出される** | 不採用 |
| Artifact | 無料 | ダウンロードのみ | 弱い | 保持期限あり | 補助（障害時のデバッグ用のみ） |

**採用: Git リポジトリ自体を正本**。実験の書き込みは 10 分に 1 回・数十 KB であり、
Git で十分です。外部サービスの認証情報が一切不要になるため、「公開ダッシュボードに
接続文字列が混入する」リスクも構造的にゼロになります。

整合性の担保:

- `state/state.json` は一時ファイル + `os.replace` で**原子的に差し替え**
- `Store.transaction()` 内で例外が起きたら**ディスクには一切書かない**（部分更新なし）
- `processed_tick_ids` により、**同じ 10 分枠の再実行では売買しない**（Actions の再試行対策）
- `experiment` ブロックは初回のみ生成し、以後上書きしない → **初期資金は再設定されない**
- push 衝突時は `pull --rebase --autostash` で最大 3 回リトライ
- 状態の読み込み・保存に失敗した場合は売買を中止（終了コード 2）

正本: `state/state.json`。派生物: `state/trades.csv`, `state/market.csv`, `docs/data.json`,
`docs/report.html`, `reports/final_report.*`。

---

## 5. セットアップ手順（ユーザー作業）

### 5.1 リポジトリ（設定済み）

本プロジェクトは **private リポジトリ `lily46/btc-paper-sim`** にセットアップ済みです。
新しく別のリポジトリに置く場合:

```bash
cd btc-sim
git init -b main
git add .
git commit -m "Add bitcoin paper-trading simulator"
gh repo create <リポジトリ名> --private --source=. --remote=origin --push
```

> 既存リポジトリに入れる場合は、このディレクトリの中身をリポジトリ直下にコピーしてください
> （`.github/workflows/`, `src/`, `docs/`, `config.json` など）。既存ファイルは削除しません。

### 5.2 GitHub Secrets の設定 → **不要です**

この構成では **Secrets は 1 つも必要ありません**。
取引所の認証情報を使わず、永続化も同じリポジトリへの commit で行うため、
Actions が自動で発行する `GITHUB_TOKEN` だけで完結します。

ただし **Workflow の書き込み権限**だけは有効化が必要です（本リポジトリでは設定済み）:

1. GitHub のリポジトリ → **Settings → Actions → General**
2. **Workflow permissions** を **Read and write permissions** に変更して Save
   （CLI なら `gh api --method PUT repos/<owner>/<repo>/actions/permissions/workflow -f default_workflow_permissions=write`）

（Workflow 側でも `permissions: contents: write` を宣言済みです。）

<details>
<summary>将来 Supabase 等に切り替える場合の Secrets 設定手順</summary>

1. Settings → **Secrets and variables → Actions → New repository secret**
2. `SUPABASE_DB_URL` = `postgresql://...`（接続文字列）を登録
3. Workflow の `Run one simulation tick` ステップに
   `env: SUPABASE_DB_URL: ${{ secrets.SUPABASE_DB_URL }}` を追加
4. `store.py` の読み書きを差し替える（インターフェースは `load()` / `transaction()` / `is_tick_processed()` の 3 つ）

**注意**: 接続文字列は `docs/`（公開される）に絶対に書き込まないこと。
`dashboard.py` は公開して問題のない設定のみをホワイトリストで出力しています。
</details>

### 5.3 ダッシュボードの閲覧（private リポジトリ運用）

private リポジトリでは GitHub Pages が使えません（GitHub Pro 以上が必要）。
そのため **データ埋め込み済みの単体 HTML** を毎ティック生成しています。

```bash
cd btc-sim
git pull                 # 最新の状態を取得
start docs/report.html   # Windows（mac は open、Linux は xdg-open）
```

- `docs/report.html` … **ダブルクリックで開ける自己完結 HTML**（ダッシュボード本体）。
  ネットワーク通信をしないため `file://` でもグラフ・表がすべて表示されます。
- `docs/index.html` + `docs/data.json` … Pages / Web サーバー用（将来 public 化や
  GitHub Pro にした場合はこちらをそのまま公開できます）。UI はこの 1 ファイルが
  テンプレートで、`report.html` はこれにデータを埋め込んで生成されます。

> `file://` で `index.html` を開いても動きません（ブラウザが `fetch("data.json")` を
> ブロックするため）。ローカル閲覧には必ず `report.html` を使ってください。
> Web サーバー経由で見たい場合は `cd docs && python -m http.server` → `localhost:8000`。

将来 public 化して Pages で公開する場合: Settings → Pages → Source = `Deploy from a branch`
→ Branch = `main` / フォルダ = `/docs`。

### 5.4 Actions の有効化

- **Settings → Actions → General → Allow all actions** を有効化
- fork したリポジトリでは、**Actions タブ** で一度 Workflow を手動有効化してください
- GitHub は 60 日間コミットが無いリポジトリの cron を停止します（本実験は 7 日なので影響なし）

---

## 6. 起動・停止・再開

| 操作 | 方法 |
|---|---|
| **初回起動（実験開始）** | Actions タブ → `paper-trade-tick` → **Run workflow**。最初の実行で `state/state.json` が作られ、開始時刻・終了予定時刻・各戦略 100 万円が確定します。以後は cron が 10 分ごとに自動実行。 |
| **一時停止** | Settings → **Secrets and variables → Actions → Variables** で `BTCSIM_PAUSED` = `true` を作成。Workflow はスキップされます（状態は保持）。 |
| **再開** | `BTCSIM_PAUSED` を `false` にするか削除。次の cron から再開し、**初期資金や開始時刻は再設定されません**。停止中のティックは `missed_schedules` に計上されます。 |
| **完全停止** | Actions タブ → `paper-trade-tick` → 右上 `…` → **Disable workflow**。 |
| **やり直し（新しい実験を開始）** | `state/state.json` を削除して commit。次の実行で新しい実験が初期化されます（**過去の記録は git 履歴に残ります**）。 |
| **手動で 1 ティック実行** | **Run workflow** →（必要なら `force: true` で同一 10 分枠でも強制実行） |
| **集計のみ再生成** | **Run workflow** → `report_only: true`（市場データ取得も売買もしません） |

ローカルでの動作確認（git へ push しない）:

```bash
cd btc-sim
PYTHONPATH=src python -m btcsim.run --root /tmp/btcsim-local --verbose     # 1 ティックだけ試す
PYTHONPATH=src python -m btcsim.run --root /tmp/btcsim-local --report-only # 集計だけ
```

`--root` を別ディレクトリにすれば、本番の `state/` を汚しません。

---

## 7. 結果の確認

| 見るもの | 場所 |
|---|---|
| **ダッシュボード（推奨）** | `git pull` して `docs/report.html` をダブルクリック |
| 各実行の判断理由つきサマリー | GitHub の **Actions → 各実行 → Summary**（なぜ買わなかったかまで表示） |
| 最終レポート | `reports/final_report.html` / `.md` / `.json` / `.csv` |
| 生データ（正本） | `state/state.json`、派生: `state/trades.csv`, `state/market.csv` |

`docs/report.html` の表示項目: 実験開始/終了予定時刻・残り時間・最終データ取得時刻・
各戦略の総資産 / JPY 残高 / BTC 保有量 / 評価損益 / 実現損益 / 売買回数 / 累計手数料 /
最大ドローダウン・BTC 価格推移・戦略別資産推移・売買履歴・Buy & Hold との差。
冒頭に「仮想売買である」ことを常時表示します。

## 8. 7 日後の集計方法

168 時間が経過すると、**次のティックで自動的に**以下が実行されます（手動操作は不要）:

1. `experiment.status` を `finished` に変更 → **以降は新規の仮想売買を行わない**
2. `reports/final_report.html` / `.md` / `.json` / `.csv` を生成して commit
   （HTML 版は終了時点の固定スナップショット。`docs/report.html` は常に最新を表示）
3. ダッシュボードが「終了」表示に切り替わる
4. 以降の cron 実行は、市場データ取得も売買もせず**数秒で終了**（無料枠の無駄打ちを回避）
5. 終了後に勝手に再スタートすることはありません（`status: finished` が残り続けます）

レポートの内容: 各戦略の最終総資産 / 損益額・損益率 / BTC 騰落率 / Buy & Hold との差 /
売買回数 / 手数料総額 / 最大ドローダウン / 勝率（算出方法付き）/ API エラー回数 /
スケジュール実行漏れ回数 / 改善すべき点。

手動で作り直したい場合は **Run workflow → `report_only: true`**、または
`PYTHONPATH=src python -m btcsim.run --report-only`。

**勝率の定義**: 売却（決済）1 回を 1 トレードとし、手数料を含む実現損益がプラスだった割合
= `wins / (wins + losses)`。未決済ポジションと実現損益ちょうど 0 の決済は分母から除外。
Buy & Hold は決済が無いため `-` 表示。

---

## 9. テスト

```bash
pip install -r requirements-dev.txt
PYTHONPATH=src python -m pytest -q      # 50 件
```

カバーしている内容（ネットワークには接触しません）:

- SMA / RSI の値、データ不足時に指標を返さないこと、足の集約とバケット境界
- **確定足のみを使うこと（ルックアヘッド検証）**
- 手数料・スリッページ・板を歩く約定、1 回 20% 上限、BTC 比率 80% 上限、
  残高超過不可、空売り不可、最低注文数量、数量精度、薄い板での部分約定
- 実現損益・勝敗カウント、最大ドローダウン 10% での停止
- 各戦略のシグナル（ゴールデン/デッドクロス、RSI の出入り、トレンドフィルタ、
  売られすぎ継続時に連打しないこと、D が 1 回しか買わないこと）
- 状態のロールバック・破損 JSON / スキーマ不一致で取引中止・**同一ティック再実行で二重売買しない**
- 再開時に初期資金と開始時刻を再設定しない、実験終了後に再スタートしない
- API エラー時・異常値検出時に売買しない
- レポート 4 形式（md/json/csv/html）の生成、単体 HTML がデータ埋め込み済みであること、ダッシュボードの表示項目契約、**公開物に認証情報が無いこと**
- **ソース内に実注文系エンドポイントが存在しないこと**

---

## 10. 既知の限界

1. 足の高値・安値・出来高は 10 分間隔の実行で取得できる約定履歴に依存するため完全ではありません。
2. ウォームアップ足のみ別取引所（bitbank）の価格を代理利用しており、わずかな差異があります。
3. 約定は「板を歩いた VWAP + 固定スリッページ」の近似で、レイテンシやマーケットインパクトは
   再現していません。
4. GitHub Actions の cron は遅延・欠落するため、足の確定直後に判定できない場合があります
   （`missed_schedules` で可視化）。
5. 7 日間・各戦略 1 パラメータセットのみの結果であり、統計的な優劣の証明にはなりません。

---

## ライセンス / 免責

本リポジトリは教育・検証目的の**シミュレーション**です。表示される損益はすべて仮想のもので、
投資助言ではありません。実際の取引に利用した結果について作者は責任を負いません。
