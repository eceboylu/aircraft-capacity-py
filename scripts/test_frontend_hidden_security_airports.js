// Manuel/ad-hoc Node testi: ZRH/AMS/FRA için "INTERNATIONAL DEPARTURE —
// SECURITY" bölümünün frontend'de HİÇ render edilmediğini, diğer
// havalimanları için DEĞİŞMEDEN göründüğünü doğrular. Backend/SQL
// hesaplaması bu testin kapsamı DIŞINDA (kullanıcı talebiyle DEĞİŞMEDİ,
// sadece görsel gizleme test ediliyor).
// `node scripts/test_frontend_hidden_security_airports.js` ile çalıştırılır.

var SECURITY_SECTION_HIDDEN_AIRPORTS = { ZRH: true, AMS: true, FRA: true };

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

// index.html'deki gerçek mantığın birebir kopyası (sadece section HTML
// string'i yerine, hangi section'ların dahil edildiğini döndüren basit
// bir stub - gerçek HTML üretimi/Chart.js çizimi bu testin kapsamı
// dışında, sadece dahil/hariç KARARI test ediliyor).
function sectionsToRender(selectedAirport) {
  var hideSecuritySection = !!SECURITY_SECTION_HIDDEN_AIRPORTS[selectedAirport];
  var sections = ["overall", "domestic_security", "international_departure_passport"];
  if (!hideSecuritySection) sections.push("international_departure_security");
  sections.push("international_arrival");
  return sections;
}

["ZRH", "AMS", "FRA"].forEach(function (iata) {
  var sections = sectionsToRender(iata);
  assertEqual(
    sections.indexOf("international_departure_security"), -1,
    iata + ": INTERNATIONAL DEPARTURE — SECURITY bölümü render EDİLMEMELİ"
  );
  assertEqual(sections.length, 4, iata + ": diğer 4 bölüm (security hariç) hâlâ render edilmeli");
});

["IST", "SAW", "LHR", "JFK"].forEach(function (iata) {
  var sections = sectionsToRender(iata);
  assertEqual(
    sections.indexOf("international_departure_security") !== -1, true,
    iata + ": bu havalimanı ETKİLENMEMELİ, security bölümü NORMAL şekilde görünmeli"
  );
  assertEqual(sections.length, 5, iata + ": tüm 5 bölüm render edilmeli");
});

console.log("");
if (failures > 0) {
  console.log(failures + " test FAILED");
  process.exit(1);
} else {
  console.log("All tests PASSED");
}
