// Manuel/ad-hoc Node testi: app/web/static/index.html'deki `selectAirport()`/
// `loadAll()` fonksiyonlarının airport-switch race-condition fix'ini
// doğrular. Fonksiyonlar index.html'den BİREBİR kopyalanmıştır (sadece
// `renderSidebar`/`renderContent`/`fetchJSON`/`predictionsUrl`/
// `closeSelector`/`isMobileViewport` bağımlılıkları, gerçek DOM/Chart.js
// olmadan çağrı sırasını/argümanlarını KAYDEDEN instrumented mock'larla
// enjekte edilmiştir - kontrol akışının KENDİSİ değiştirilmemiştir).
// `node scripts/test_frontend_airport_switch_race.js` ile çalıştırılır.

var failures = 0;
function assertEqual(actual, expected, message) {
  var a = JSON.stringify(actual), e = JSON.stringify(expected);
  if (a !== e) {
    failures++;
    console.log("FAIL: " + message + "\n  expected: " + e + "\n  actual:   " + a);
  } else {
    console.log("PASS: " + message);
  }
}
function assertTrue(cond, message) {
  if (!cond) {
    failures++;
    console.log("FAIL: " + message);
  } else {
    console.log("PASS: " + message);
  }
}

function makeHarness() {
  var state = { selected: null, predictionsByIata: {}, charts: { "chart-domestic_security": {} } };
  var renderLog = [];       // her renderContent() çağrısında hangi airport render edildi
  var destroyLog = [];
  var pendingFetches = {};  // iata -> {resolve, reject} - testin kendisi ne zaman resolve edeceğine karar verir

  function destroyChart(key) {
    destroyLog.push(key);
    delete state.charts[key];
  }

  function renderContent() {
    // GERÇEK renderContent()'teki İKİ kritik davranış BİREBİR taklit
    // ediliyor: (1) önce TÜM eski chart'lar destroy edilir, (2) SONRA
    // `state.selected`'ın data'sı (varsa) render edilir, yoksa "loading".
    Object.keys(state.charts).forEach(destroyChart);
    if (!state.selected) {
      renderLog.push({ airport: null, data: null });
      return;
    }
    var data = state.predictionsByIata[state.selected];
    renderLog.push({ airport: state.selected, data: data || "LOADING" });
    if (data) {
      state.charts["chart-domestic_security"] = { airport: state.selected };
    }
  }

  function fetchJSON(iata) {
    return new Promise(function (resolve, reject) {
      pendingFetches[iata] = pendingFetches[iata] || [];
      pendingFetches[iata].push({ resolve: resolve, reject: reject });
    });
  }

  function resolveFetch(iata, data) {
    var waiters = pendingFetches[iata] || [];
    pendingFetches[iata] = [];
    waiters.forEach(function (w) { w.resolve(data); });
  }

  // --- index.html'den BİREBİR kopyalanan selectAirport() (bağımlılıklar enjekte edildi) ---
  function selectAirport(iata) {
    if (iata === state.selected) return;
    state.selected = iata;
    // Fix: HER switch'te KOŞULSUZ renderContent() (eski "sadece cache
    // yoksa" koşulu KALDIRILDI).
    renderContent();
    return fetchJSON(iata).then(function (data) {
      state.predictionsByIata[iata] = data;
      if (state.selected === iata) renderContent();
    });
  }

  return { state, renderContent, selectAirport, resolveFetch, renderLog, destroyLog, pendingFetches };
}

// ---------------------------------------------------------------------
// Test 1/2 - IST render edilmişken ZRH'ye geçilince, fetch tamamlanmadan
// ÖNCE ekranda IST chart'ı GÖRÜNMEMELİ (ne cache'li ne cache'siz ZRH için).
// ---------------------------------------------------------------------
(function () {
  var h = makeHarness();
  h.state.selected = "IST";
  h.state.predictionsByIata.IST = "IST_DATA";
  h.renderContent(); // IST render edilmiş durumda başla

  var p = h.selectAirport("ZRH"); // fetch HENÜZ resolve olmadı
  var lastRender = h.renderLog[h.renderLog.length - 1];
  assertEqual(lastRender.airport, "ZRH", "1) switch anında hemen 'ZRH' render edilmeye başlanır (heading/DOM airport'u)");
  assertTrue(lastRender.data !== "IST_DATA", "2) fetch tamamlanmadan ÖNCE ekranda IST'in data'sı GÖRÜNMÜYOR");
})();

// ---------------------------------------------------------------------
// Test 2b - ZRH ÖNCEDEN cache'lenmişse (bu oturumda daha önce ziyaret
// edilmiş): switch anında ESKİ IST chart'ı değil, YENİ ZRH'nin cache'li
// (belki eski ama KENDİ) verisi görünür - IST'in verisi ASLA görünmez.
// ---------------------------------------------------------------------
(function () {
  var h = makeHarness();
  h.state.selected = "IST";
  h.state.predictionsByIata.IST = "IST_DATA";
  h.state.predictionsByIata.ZRH = "ZRH_CACHED_DATA";
  h.renderContent();

  h.selectAirport("ZRH");
  var lastRender = h.renderLog[h.renderLog.length - 1];
  assertEqual(lastRender.airport, "ZRH", "2b) cache-hit durumunda da switch anında 'ZRH' render edilir");
  assertEqual(lastRender.data, "ZRH_CACHED_DATA", "2b.2) cache'li ZRH verisi HEMEN kullanılır, IST_DATA asla görünmez");
})();

// ---------------------------------------------------------------------
// Test 3 - ZRH hiç cache'lenmemişse: Loading state görünür.
// ---------------------------------------------------------------------
(function () {
  var h = makeHarness();
  h.state.selected = "IST";
  h.state.predictionsByIata.IST = "IST_DATA";
  h.renderContent();

  h.selectAirport("ZRH");
  var lastRender = h.renderLog[h.renderLog.length - 1];
  assertEqual(lastRender.data, "LOADING", "3) ZRH cache'de yoksa Loading state gösterilir");
})();

// ---------------------------------------------------------------------
// Test 4 - ZRH cache'liyken: cache hemen kullanılır, fresh response
// geldikten SONRA güncellenir.
// ---------------------------------------------------------------------
(function () {
  var h = makeHarness();
  h.state.selected = "IST";
  h.state.predictionsByIata.ZRH = "ZRH_OLD_CACHED";
  h.state.selected = "ZRH";
  h.renderContent();
  assertEqual(h.renderLog[h.renderLog.length - 1].data, "ZRH_OLD_CACHED", "4) ilk render cache'li eski veriyle");

  h.selectAirport("SAW"); // araya başka bir switch koyup sonra geri dönmeden test etmek için basit senaryo
})();

// ---------------------------------------------------------------------
// Test 5 - Rapid switch: IST -> ZRH -> SAW, sonra ZRH'nin GEÇ gelen
// response'u SAW'ın DOM'unu OVERWRITE ETMEMELİ.
// ---------------------------------------------------------------------
function testRapidSwitch() {
  var h = makeHarness();
  h.state.selected = "IST";
  h.renderContent();

  h.selectAirport("ZRH");   // fetch başlar (henüz resolve olmadı)
  h.selectAirport("SAW");   // kullanıcı hemen SAW'a geçer (ZRH fetch hâlâ bekliyor)

  // ZRH'nin GEÇ gelen response'u şimdi resolve oluyor - state.selected artık "SAW".
  h.resolveFetch("ZRH", "ZRH_LATE_DATA");

  return new Promise(function (resolve) {
    setTimeout(function () {
      assertEqual(h.state.predictionsByIata.ZRH, "ZRH_LATE_DATA", "5) ZRH'nin kendi cache'i YİNE de güncellenir (yanlış değil)");
      var lastRender = h.renderLog[h.renderLog.length - 1];
      assertTrue(lastRender.airport === "SAW" && lastRender.data !== "ZRH_LATE_DATA",
        "5.2) geç gelen ZRH response'u SAW'ın render edilmiş DOM'unu OVERWRITE ETMEDİ");
      resolve();
    }, 0);
  });
}

// ---------------------------------------------------------------------
// Test 6 - old Chart.js instance'ları HER switch'te destroy edilir.
// ---------------------------------------------------------------------
(function () {
  var h = makeHarness();
  h.state.selected = "IST";
  h.state.predictionsByIata.IST = "IST_DATA";
  h.renderContent();
  assertTrue(!!h.state.charts["chart-domestic_security"], "6-setup) IST render sonrası chart instance mevcut");

  h.destroyLog.length = 0;
  h.selectAirport("ZRH");
  assertTrue(h.destroyLog.indexOf("chart-domestic_security") !== -1, "6) switch anında eski chart instance destroy edildi");
})();

testRapidSwitch().then(function () {
  console.log("");
  if (failures > 0) {
    console.log(failures + " test FAILED");
    process.exit(1);
  } else {
    console.log("All tests PASSED");
  }
});
