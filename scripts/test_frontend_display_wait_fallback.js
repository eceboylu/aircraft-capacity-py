// Manuel/ad-hoc Node testi: app/web/static/index.html'deki `displayPoints()`/
// `EVENT_DRIVEN_DISPLAY_KEYS` mantığının, 4 event-driven süreç (security_dom,
// security_intl, passport_dep, passport_arr - frontend key'leriyle:
// domestic_security/international_departure_security/international_
// departure_passport/international_arrival) için HİÇBİR ZAMAN saatlik
// `windows` (queue_predictions.estimated_wait_minutes - "o saate girenin
// TAM deneyimlediği wait", FARKLI bir metrik) fallback'ine sessizce
// düşmediğini doğrular. Fonksiyonlar index.html'den BİREBİR kopyalanmıştır.
// `node scripts/test_frontend_display_wait_fallback.js` ile çalıştırılır.

var EVENT_DRIVEN_DISPLAY_KEYS = {
  domestic_security: true,
  international_departure_security: true,
  international_departure_passport: true,
  international_arrival: true,
};

function to30MinuteVisualBucketsStub(points) {
  // Bu testte 30dk agregasyonun kendisi değil, fallback SEÇİMİ test
  // ediliyor - display_5m varsa aynen (kopyalanmadan) geri döndürülüyor.
  return points;
}

function displayPoints(section, key) {
  if (section.display_5m && section.display_5m.length) {
    if (!section._visualBuckets) {
      section._visualBuckets = to30MinuteVisualBucketsStub(section.display_5m);
    }
    return section._visualBuckets;
  }
  if (key != null && EVENT_DRIVEN_DISPLAY_KEYS[key]) {
    return [];
  }
  return section.windows || [];
}

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

var fakeWindows = [{ window_start: "2026-09-29T14:00:00", estimated_wait_minutes: 321.1 }];
var fakeDisplay5m = [{ window_start: "2026-09-29T16:45:00", estimated_wait_minutes: 170.5 }];

// ---------------------------------------------------------------------
// A) Event-driven süreçlerin 4'ü de: display_5m boşsa/hiç yoksa -> []
//    (saatlik windows/hourly-experienced-wait'e ASLA sessizce düşmez)
// ---------------------------------------------------------------------
["domestic_security", "international_departure_security",
 "international_departure_passport", "international_arrival"].forEach(function (key) {
  assertEqual(
    displayPoints({ display_5m: [], windows: fakeWindows }, key), [],
    "A) " + key + ": display_5m boş -> [] (windows'a DÜŞMEZ)"
  );
  assertEqual(
    displayPoints({ windows: fakeWindows }, key), [],
    "A.2) " + key + ": display_5m hiç yok -> [] (windows'a DÜŞMEZ)"
  );
});

// ---------------------------------------------------------------------
// B) Event-driven süreçlerin 4'ü de: display_5m doluysa -> HEP display_5m
//    kullanılır (windows mevcut olsa bile GÖRMEZDEN GELİNİR).
// ---------------------------------------------------------------------
["domestic_security", "international_departure_security",
 "international_departure_passport", "international_arrival"].forEach(function (key) {
  var section = { display_5m: fakeDisplay5m, windows: fakeWindows };
  assertEqual(
    displayPoints(section, key), fakeDisplay5m,
    "B) " + key + ": display_5m doluysa windows YERİNE display_5m kullanılır"
  );
});

// ---------------------------------------------------------------------
// C) Event-driven OLMAYAN bir bölüm (ör. "overall"): display_5m yoksa
//    windows fallback'i KORUNUR (bu davranış DEĞİŞMEDİ).
// ---------------------------------------------------------------------
assertEqual(
  displayPoints({ windows: fakeWindows }, "overall"), fakeWindows,
  "C) overall: display_5m yok -> windows fallback KORUNDU"
);
assertEqual(
  displayPoints({ windows: fakeWindows }, undefined), fakeWindows,
  "C.2) key verilmezse (eski çağrı sözleşmesi) -> windows fallback KORUNDU"
);

// ---------------------------------------------------------------------
// D) Gerçek queue tamamen boşsa (display_5m dolu ama tüm noktalar
//    passenger_count=0/wait=0): bu GERÇEK bir "queue boş" durumu, []
//    dönmemeli - display_5m'in KENDİSİ [] DEĞİL, içindeki noktalar 0.
// ---------------------------------------------------------------------
var trulyEmptyQueuePoints = [{ window_start: "2026-09-29T03:00:00", estimated_wait_minutes: 0.0, passenger_count: 0 }];
assertEqual(
  displayPoints({ display_5m: trulyEmptyQueuePoints, windows: fakeWindows }, "domestic_security"),
  trulyEmptyQueuePoints,
  "D) display_5m dolu (0 değerli noktalarla) -> aynen kullanılır, [] veya windows'a düşülmez"
);

// ---------------------------------------------------------------------
// E) `pickCurrentPoint()`: display_5m boşsa event-driven süreçlerde
//    backend'in `section.current`'ına (o da `windows`'tan seçilir) DA
//    düşülmemeli - null döner ("veri yok").
// ---------------------------------------------------------------------
function pickCurrentPoint(section, key) {
  if (section.display_5m && section.display_5m.length) {
    var points = displayPoints(section, key);
    return points[0] || null;
  }
  if (key != null && EVENT_DRIVEN_DISPLAY_KEYS[key]) {
    return null;
  }
  return section.current || null;
}

assertEqual(
  pickCurrentPoint({ windows: fakeWindows, current: fakeWindows[0] }, "international_arrival"),
  null,
  "E) passport_arr: display_5m yok -> section.current'a da DÜŞMEZ, null"
);
assertEqual(
  pickCurrentPoint({ windows: fakeWindows, current: fakeWindows[0] }, "overall"),
  fakeWindows[0],
  "E.2) overall (event-driven değil): section.current fallback'i KORUNDU"
);

console.log("");
if (failures > 0) {
  console.log(failures + " test FAILED");
  process.exit(1);
} else {
  console.log("All tests PASSED");
}
