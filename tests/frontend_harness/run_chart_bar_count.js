/*
 * ADIM (Current-Day Graph Leakage - Global Audit) - Section 12.
 *
 * `run_render.js`'in AYNI (kanıtlanmış, değiştirilmedi) DOM/eval kurulumunu
 * kullanır, ama `Chart` stub'ı burada her çağrının `config.data.labels`
 * dizisini KAYDEDER - "drawChart() gerçekten kaç bar çiziyor" sorusuna,
 * statik string-match DEĞİL, index.html'in KENDİ `drawChart()` fonksiyonunu
 * GERÇEKTEN çalıştırarak cevap verir.
 *
 * Kullanım:
 *   node run_chart_bar_count.js <path-to-index.html> <path-to-data.json>
 *
 * stdout: tek satır JSON - {"<canvasId>": {"count": N, "labels": [...]}, ...}
 * exit code: 0 (patlamadı) / 1 (exception).
 */
const fs = require("fs");

const indexHtmlPath = process.argv[2];
const dataJsonPath = process.argv[3];

const html = fs.readFileSync(indexHtmlPath, "utf-8");
let script = html.split("<script>")[1].split("</script>")[0];

script = script.replace(
  /\n\s*wireAirportSelector\(\);\s*\n\s*loadAll\(\);\s*\n\s*setInterval\(loadAll, POLL_MS\);\s*\n/,
  "\n",
);

script = script.replace(
  /\}\)\(\);\s*$/,
  "  globalThis.__harness = { renderContent, state };\n})();"
);
if (!script.includes("__harness")) {
  console.log("THREW: harness injection failed - script sonu eşleşmedi");
  process.exit(1);
}

const elements = {};
function makeEl(id) {
  return {
    id: id,
    _html: "",
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    textContent: "",
    style: {},
    querySelectorAll: function () { return []; },
    addEventListener: function () {},
    getContext: function () { return {}; },
  };
}
global.document = {
  getElementById: function (id) {
    if (!elements[id]) elements[id] = makeEl(id);
    return elements[id];
  },
  addEventListener: function () {},
  createElement: function () {
    return {
      set textContent(v) { this._t = v; },
      get innerHTML() { return this._t; },
    };
  },
};
global.window = global;

// Bar-kaydeden Chart stub - hangi canvas'a hangi label dizisiyle
// cizildigini yakalar (canvas.id -> {labels, dataLength}).
const chartCalls = {};
global.Chart = function (canvas, config) {
  const id = canvas && canvas.id ? canvas.id : "unknown";
  const labels = (config && config.data && config.data.labels) || [];
  const data0 = (config && config.data && config.data.datasets && config.data.datasets[0] && config.data.datasets[0].data) || [];
  chartCalls[id] = { labels: labels.slice(), count: labels.length, dataCount: data0.length };
  this.destroy = function () {};
};
global.fetch = function () {
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(null); } });
};

try {
  eval(script);
} catch (e) {
  console.log("THREW (eval-time):", e && e.stack ? e.stack : e);
  process.exit(1);
}

const data = JSON.parse(fs.readFileSync(dataJsonPath, "utf-8"));

try {
  globalThis.__harness.state.selected = data.airport || "TST";
  globalThis.__harness.state.predictionsByIata = {};
  globalThis.__harness.state.predictionsByIata[globalThis.__harness.state.selected] = data;
  globalThis.__harness.renderContent();
  console.log("OK");
  console.log(JSON.stringify(chartCalls));
  process.exit(0);
} catch (e) {
  console.log("THREW:", e && e.stack ? e.stack : e);
  process.exit(1);
}
