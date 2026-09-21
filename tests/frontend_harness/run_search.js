/*
 * ADIM (Frontend Airport Search) - `index.html`'in GERÇEK arama/dropdown
 * mantığını (normalizeSearchText/filteredDirectory/openSelector/
 * closeSelector/selectAirport/renderSidebar) bir Node ortamında ÇALIŞTIRIP
 * doğrular - `run_render.js` ile AYNI desen (string-match DEĞİL, gerçek
 * JS execution).
 *
 * Kullanım:
 *   node run_search.js <path-to-index.html> <scenario.json>
 *
 * scenario.json şekli:
 *   { "directory": [{"iata":"IST","name":"Istanbul Airport"}, ...],
 *     "viewport": "desktop" | "mobile",   // opsiyonel, varsayılan "desktop"
 *     "actions": [
 *        {"type": "setQuery", "value": "istanbul"},
 *        {"type": "openSelector"},
 *        {"type": "closeSelector"},
 *        {"type": "selectAirport", "iata": "IST"}
 *     ] }
 *
 * stdout: JSON { ok: true, filtered: [...], selectorOpen: bool,
 *                selected: str|null, labelHtml: str, listHtml: str }
 *         veya "THREW: ..." + hata.
 */
const fs = require("fs");

const indexHtmlPath = process.argv[2];
const scenarioPath = process.argv[3];

const html = fs.readFileSync(indexHtmlPath, "utf-8");
let script = html.split("<script>")[1].split("</script>")[0];
script = script.replace(/\n\s*loadAll\(\);\s*\n\s*setInterval\(loadAll, POLL_MS\);\s*\n/, "\n");
script = script.replace(
  /\}\)\(\);\s*$/,
  "  globalThis.__harness = { state, normalizeSearchText, filteredDirectory, openSelector, closeSelector, selectAirport, renderSidebar, wireAirportSelector, isMobileViewport };\n})();"
);
if (!script.includes("__harness")) {
  console.log("THREW: harness injection failed");
  process.exit(1);
}

function makeClassList() {
  const set = new Set();
  return {
    add: (c) => set.add(c),
    remove: (c) => set.delete(c),
    contains: (c) => set.has(c),
  };
}

function makeEl(id) {
  const attrs = {};
  return {
    id,
    _html: "",
    value: "",
    classList: makeClassList(),
    set innerHTML(v) { this._html = v; },
    get innerHTML() { return this._html; },
    textContent: "",
    style: {},
    querySelectorAll: () => [],
    addEventListener: () => {},
    contains: () => false,
    getContext: () => ({}),
    focus: () => {},
    blur: () => {},
    getAttribute: (name) => attrs[name],
    setAttribute: (name, val) => { attrs[name] = val; },
  };
}

const scenario = JSON.parse(fs.readFileSync(scenarioPath, "utf-8"));
const isMobile = scenario.viewport === "mobile";

const elements = {};
global.document = {
  getElementById: (id) => {
    if (!elements[id]) elements[id] = makeEl(id);
    return elements[id];
  },
  createElement: () => ({
    set textContent(v) { this._t = v; },
    get innerHTML() { return this._t; },
  }),
  addEventListener: () => {},
};
global.window = global;
// `isMobileViewport()` (index.html) `window.matchMedia("(max-width: 760px)").matches`
// okur - senaryonun `viewport` alanına göre GERÇEK bir stub sağlanır
// (varsayılan "desktop" - matches=false).
global.window.matchMedia = (query) => ({ matches: isMobile });
global.Chart = function () { this.destroy = function () {}; };
global.fetch = () => Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(null) });

try {
  eval(script);
} catch (e) {
  console.log("THREW (eval-time):", e && e.stack ? e.stack : e);
  process.exit(1);
}

const h = globalThis.__harness;

try {
  h.state.directory = scenario.directory || [];
  h.renderSidebar();

  for (const action of scenario.actions || []) {
    if (action.type === "setQuery") {
      h.state.searchQuery = action.value;
      h.renderSidebar();
    } else if (action.type === "openSelector") {
      h.openSelector();
    } else if (action.type === "closeSelector") {
      h.closeSelector();
      h.renderSidebar();
    } else if (action.type === "selectAirport") {
      h.selectAirport(action.iata);
    } else {
      throw new Error("unknown action type: " + action.type);
    }
  }

  const result = {
    ok: true,
    filtered: h.filteredDirectory().map((e) => e.iata),
    selectorOpen: h.state.selectorOpen,
    selected: h.state.selected,
    searchQuery: h.state.searchQuery,
    labelHtml: document.getElementById("selected-airport-label").innerHTML,
    listHtml: document.getElementById("airport-list").innerHTML,
    selectorOpenClass: document.getElementById("airport-selector").classList.contains("open"),
    isMobileViewport: h.isMobileViewport(),
  };
  console.log(JSON.stringify(result));
  process.exit(0);
} catch (e) {
  console.log("THREW:", e && e.stack ? e.stack : e);
  process.exit(1);
}
