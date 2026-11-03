// docs/report.html の描画ロジックを Node 上で実行して検証する補助スクリプト。
// 使い方: node tests/render_check.js <report.html のパス> [spot|fx]
// （pytest からは呼ばれない。Node があるときの手動検証用。）
const fs = require("fs");

const file = process.argv[2];
const html = fs.readFileSync(file, "utf8");
const script = html.split("<script>")[1].split("</script>")[0];

const nodes = {};
function makeNode(id) {
  return {
    id, innerHTML: "", textContent: "",
    clientWidth: 900,
    attrs: {},
    setAttribute(k, v) { this.attrs[k] = v; },
  };
}
for (const id of ["meta", "gen", "status", "cards", "priceChart", "equityChart",
                  "equityLegend", "table", "trades", "bySide", "cmp", "cmpNote",
                  "common", "commonNote"]) {
  nodes[id] = makeNode(id);
}
global.document = {
  getElementById: (id) => nodes[id] ?? makeNode(id),
  createElement: () => ({ set innerHTML(v) { this._v = v; }, content: { firstChild: null } }),
};
global.fetch = () => { throw new Error("fetch は呼ばれてはいけない（埋め込みデータを使うはず）"); };

new Function(script)();

setTimeout(() => {
  const fail = (m) => { console.error("FAIL: " + m); process.exit(1); };
  if (nodes.status.innerHTML.includes("読み込みエラー")) fail("読み込みエラーになった: " + nodes.status.innerHTML);
  const kind = process.argv[3] || "spot";
  const expect = kind === "fx"
    ? [["status", "実験期間"], ["cards", "レバレッジ・ロング放置"], ["table", "最大DD%"],
       ["trades", "新規"], ["equityLegend", "モメンタム"], ["meta", "FX_BTC_JPY"],
       ["bySide", "方向"]]
    : [["status", "実験期間"], ["cards", "Buy & Hold"], ["table", "最大DD%"],
       ["trades", "BUY"], ["equityLegend", "移動平均"], ["meta", "BTC_JPY"]];
  for (const [id, needle] of expect) {
    if (!nodes[id].innerHTML.includes(needle)) fail(`#${id} に「${needle}」が無い: ${nodes[id].innerHTML.slice(0, 200)}`);
  }
  for (const id of ["priceChart", "equityChart"]) {
    const svg = nodes[id].innerHTML;
    if (!svg.includes("<path")) fail(`#${id} に折れ線が描かれていない`);
    if (svg.includes("NaN")) fail(`#${id} に NaN が含まれる`);
    if (!nodes[id].attrs.viewBox) fail(`#${id} に viewBox が無い`);
  }
  if (nodes.cards.innerHTML.includes("NaN") || nodes.table.innerHTML.includes("NaN")) fail("NaN が表示されている");
  console.log("OK: 描画検証を通過");
  console.log("  cards:", nodes.cards.innerHTML.length, "chars / trades rows:",
              (nodes.trades.innerHTML.match(/<tr>/g) || []).length);
}, 50);
