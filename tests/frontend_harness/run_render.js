/*
 * ADIM (Frontend Runtime Bug) - genel-proje.md Phase 7/9 doğrulaması.
 *
 * `app/web/static/index.html`'in gerçek <script> içeriğini, GERÇEK bir
 * JS motorunda (Node) minimal DOM/fetch/Chart stub'larıyla çalıştırıp
 * `renderContent()`'i tek bir airport'un verilen `data` payload'ıyla
 * çağırır. Amaç: index.html'i STATİK string olarak parse etmek yerine
 * GERÇEKTEN çalıştırıp uncaught exception olup olmadığını kanıtlamak -
 * string-match testleri ("graphSectionHtml(" in script) bir şeyin
 * VAR OLDUĞUNU kanıtlar, ama runtime'da GERÇEKTEN patlamadığını KANITLAMAZ.
 *
 * Kullanım:
 *   node run_render.js <path-to-index.html> <path-to-data.json>
 *
 * stdout:
 *   "OK" + rendered HTML uzunluğu, veya "THREW" + hata mesajı/stack.
 * exit code: 0 (patlamadı) / 1 (exception oluştu).
 */
const fs = require("fs");
const path = require("path");

const indexHtmlPath = process.argv[2];
const dataJsonPath = process.argv[3];

const html = fs.readFileSync(indexHtmlPath, "utf-8");
// NOT: JS'de String.split(sep, limit)'in 2. argümanı Python'daki
// maxsplit gibi DAVRANMAZ - dönen DİZİNİN boyutunu sınırlar (limit=1
// -> SADECE ilk parça döner, [1] undefined olur). Limit VERİLMEDEN
// kullanılır.
let script = html.split("<script>")[1].split("</script>")[0];

// Sayfa yüklendiğinde otomatik çalışan boot çağrılarını (`wireAirport
// Selector()`/`loadAll()`/`setInterval(...)`) KALDIR - test KENDİ
// akışını kontrol eder, gerçek ağa/timer'a hiç çıkmaz. `renderContent()`
// testleri arama/seçici davranışını KAPSAMADIĞI için (bkz.
// `run_search.js`) burada wire edilmesine hiç gerek yok.
script = script.replace(
  /\n\s*wireAirportSelector\(\);\s*\n\s*loadAll\(\);\s*\n\s*setInterval\(loadAll, POLL_MS\);\s*\n/,
  "\n",
);

// IIFE kapanışından ÖNCE, test'in erişebileceği fonksiyon/state
// referanslarını dışa aç - kaynak dosyanın KENDİSİ değiştirilmedi,
// sadece bu KOPYADA (bellekte) bir dışa-aktarma satırı eklendi.
script = script.replace(
  /\}\)\(\);\s*$/,
  "  globalThis.__harness = { renderContent, state };\n})();"
);
if (!script.includes("__harness")) {
  console.log("THREW: harness injection failed - script sonu eşleşmedi");
  process.exit(1);
}

// ---- Minimal DOM stub ----
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
// Chart.js stub - gerçek chart kütüphanesi yok, sadece constructor/destroy.
global.Chart = function () { this.destroy = function () {}; };
global.fetch = function () {
  return Promise.resolve({ ok: true, status: 200, json: function () { return Promise.resolve(null); } });
};

let evalError = null;
try {
  eval(script);
} catch (e) {
  console.log("THREW (eval-time):", e && e.stack ? e.stack : e);
  process.exit(1);
}

const data = JSON.parse(fs.readFileSync(dataJsonPath, "utf-8"));

try {
  globalThis.__harness.state.selected = "TST";
  globalThis.__harness.state.predictionsByIata = { TST: data };
  globalThis.__harness.renderContent();
  const html2 = document.getElementById("airport-content").innerHTML;
  console.log("OK length=" + html2.length);
  console.log("SNIPPET:" + html2.slice(0, 4000).replace(/\n/g, " "));
  process.exit(0);
} catch (e) {
  console.log("THREW:", e && e.stack ? e.stack : e);
  process.exit(1);
}
