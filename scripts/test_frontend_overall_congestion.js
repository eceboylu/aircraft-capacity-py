// Manuel/ad-hoc Node testi: "OVERALL AIRPORT CONGESTION" kartının artık
// departure passenger flow (queue wait DEĞİL) kullandığını doğrular.
// Repo'da bir JS test framework'ü olmadığı için (bkz. scripts/test_
// frontend_30min_aggregation.js AYNI konvansiyon), bu index.html'den
// BİREBİR kopyalanmış (yeniden yazılmamış) fonksiyonlarla çalışan,
// çalıştırılabilir bir regresyon script'idir.
// `node scripts/test_frontend_overall_congestion.js` ile çalıştırılır.

function hhmm(isoString) {
  return isoString.slice(11, 16);
}
function localHourOf(isoLocalOrUtc) {
  var label = isoLocalOrUtc ? hhmm(isoLocalOrUtc) : "";
  return label ? parseInt(label.slice(0, 2), 10) : null;
}
function localMinuteOf(isoLocalOrUtc) {
  var label = isoLocalOrUtc ? hhmm(isoLocalOrUtc) : "";
  return label ? parseInt(label.slice(3, 5), 10) : null;
}
function formatHour12(hour) {
  var h = ((hour % 24) + 24) % 24;
  var period = h < 12 ? "a" : "p";
  var display = h % 12;
  if (display === 0) display = 12;
  return display + period;
}
function addMinutesToIsoLocal(iso, minutesToAdd) {
  if (!iso) return iso;
  var datePart = iso.slice(0, 10);
  var hour = parseInt(iso.slice(11, 13), 10);
  var minute = parseInt(iso.slice(14, 16), 10);
  var tail = iso.slice(16);
  var totalMinutes = hour * 60 + minute + minutesToAdd;
  var dayOverflow = Math.floor(totalMinutes / 1440);
  var normalizedMinutes = ((totalMinutes % 1440) + 1440) % 1440;
  var newHour = Math.floor(normalizedMinutes / 60);
  var newMinute = normalizedMinutes % 60;
  var newDatePart = datePart;
  if (dayOverflow !== 0) {
    var d = new Date(datePart + "T00:00:00Z");
    d.setUTCDate(d.getUTCDate() + dayOverflow);
    newDatePart = d.toISOString().slice(0, 10);
  }
  var pad2 = function (n) { return n < 10 ? "0" + n : "" + n; };
  return newDatePart + "T" + pad2(newHour) + ":" + pad2(newMinute) + tail;
}
var RISK_UI_LABEL = { LOW: "Normal", MEDIUM: "Getting Busy", HIGH: "Busy", CRITICAL: "Very Busy", UNKNOWN: "Unknown" };

// ---- copied verbatim from index.html ----

function to30MinuteFlowBuckets(points) {
  if (!points || !points.length) return [];
  var buckets = {};
  var order = [];
  points.forEach(function (p) {
    var rawLocal = p.window_start_local || p.window_start;
    var rawUtc = p.window_start;
    var minute = localMinuteOf(rawLocal);
    if (minute == null) return;
    var bucketMinute = minute < 30 ? 0 : 30;
    var backOffset = -(minute - bucketMinute);
    var bucketIndex = (localHourOf(rawLocal) * 2) + (bucketMinute === 30 ? 1 : 0) + "@" + rawLocal.slice(0, 10);
    if (!buckets[bucketIndex]) {
      var startLocal = addMinutesToIsoLocal(rawLocal, backOffset);
      var startUtc = addMinutesToIsoLocal(rawUtc, backOffset);
      buckets[bucketIndex] = {
        window_start: startUtc,
        window_start_local: startLocal,
        window_end: addMinutesToIsoLocal(startUtc, 30),
        window_end_local: addMinutesToIsoLocal(startLocal, 30),
        total_departure_pax: 0, domestic_departure_pax: 0,
        schengen_departure_pax: 0, non_schengen_departure_pax: 0,
        flight_count: 0,
      };
      order.push(bucketIndex);
    }
    var bucket = buckets[bucketIndex];
    bucket.total_departure_pax += p.total_departure_pax || 0;
    bucket.domestic_departure_pax += p.domestic_departure_pax || 0;
    bucket.schengen_departure_pax += p.schengen_departure_pax || 0;
    bucket.non_schengen_departure_pax += p.non_schengen_departure_pax || 0;
    bucket.flight_count = Math.max(bucket.flight_count, p.flight_count || 0);
  });
  order.sort(function (a, b) { return buckets[a].window_start_local < buckets[b].window_start_local ? -1 : 1; });
  return order.map(function (idx) { return buckets[idx]; });
}

function flowCongestionThresholds(points) {
  var values = (points || [])
    .map(function (p) { return p.total_departure_pax; })
    .filter(function (v) { return v != null; })
    .sort(function (a, b) { return a - b; });
  if (!values.length) return null;
  function percentile(p) {
    var idx = Math.min(values.length - 1, Math.floor(p * values.length));
    return values[idx];
  }
  return { p50: percentile(0.50), p75: percentile(0.75), p90: percentile(0.90) };
}

function congestionRiskForFlow(pax, thresholds) {
  if (pax == null || !thresholds) return "UNKNOWN";
  if (pax <= thresholds.p50) return "LOW";
  if (pax <= thresholds.p75) return "MEDIUM";
  if (pax <= thresholds.p90) return "HIGH";
  return "CRITICAL";
}

function annotateFlowPointsWithRisk(points) {
  var thresholds = flowCongestionThresholds(points);
  points.forEach(function (p) {
    p.risk = congestionRiskForFlow(p.total_departure_pax, thresholds);
    p.risk_label = RISK_UI_LABEL[p.risk] || RISK_UI_LABEL.UNKNOWN;
  });
  return points;
}

function pickCurrentFlowPoint(points) {
  var nowMs = Date.now();
  var chosen = null;
  for (var i = 0; i < points.length; i++) {
    var raw = points[i].window_start_local || points[i].window_start;
    var ms = new Date(raw).getTime();
    if (!isNaN(ms) && ms <= nowMs) chosen = points[i];
  }
  return chosen || points[0] || null;
}

function flowPeakBestSummary(points) {
  var scored = (points || []).filter(function (p) { return p.total_departure_pax != null; });
  if (!scored.length) return null;
  var peak = scored[0];
  scored.forEach(function (p) { if (p.total_departure_pax > peak.total_departure_pax) peak = p; });
  var nonZero = scored.filter(function (p) { return p.total_departure_pax > 0; });
  var lowest = nonZero.length ? nonZero[0] : null;
  nonZero.forEach(function (p) { if (p.total_departure_pax < lowest.total_departure_pax) lowest = p; });
  var peakHour = localHourOf(peak.window_start_local || peak.window_start);
  var lowestHour = lowest ? localHourOf(lowest.window_start_local || lowest.window_start) : null;
  return {
    peakPax: Math.round(peak.total_departure_pax),
    peakHour: peakHour != null ? formatHour12(peakHour) : "",
    lowestPax: lowest ? Math.round(lowest.total_departure_pax) : null,
    lowestHour: lowestHour != null ? formatHour12(lowestHour) : "",
  };
}

// ---- tests ----

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
  if (!cond) { failures++; console.log("FAIL: " + message); }
  else { console.log("PASS: " + message); }
}

function p(hh, mm, total, dom, sch, nonSch, flights) {
  var hhStr = hh < 10 ? "0" + hh : "" + hh;
  var mmStr = mm < 10 ? "0" + mm : "" + mm;
  var iso = "2026-09-29T" + hhStr + ":" + mmStr + ":00+03:00";
  return {
    window_start: iso, window_start_local: iso,
    total_departure_pax: total, domestic_departure_pax: dom,
    schengen_departure_pax: sch, non_schengen_departure_pax: nonSch,
    flight_count: flights,
  };
}

// --------------------------------------------------------------------
// A) 30m = SUM(6x5m), not average - and breakdown totals correct
// --------------------------------------------------------------------
var fivemin = [
  p(10, 0, 100, 50, 20, 30, 5),
  p(10, 5, 120, 60, 20, 40, 6),
  p(10, 10, 110, 40, 30, 40, 5),
  p(10, 15, 90, 30, 20, 40, 4),
  p(10, 20, 80, 20, 10, 50, 3),
  p(10, 25, 70, 20, 10, 40, 3),
];
var buckets = to30MinuteFlowBuckets(fivemin);
assertEqual(buckets.length, 1, "A.1) 6 adet 5dk noktası TEK bir 30dk bucket'a düşüyor");
var expectedTotal = 100 + 120 + 110 + 90 + 80 + 70; // 570
assertEqual(buckets[0].total_departure_pax, expectedTotal, "A.2) 30m total = SUM(6x5m), ORTALAMA DEĞİL");
var expectedDom = 50 + 60 + 40 + 30 + 20 + 20; // 220
var expectedSch = 20 + 20 + 30 + 20 + 10 + 10; // 110
var expectedNonSch = 30 + 40 + 40 + 40 + 50 + 40; // 240
assertEqual(buckets[0].domestic_departure_pax, expectedDom, "A.3) domestic breakdown = SUM(6x5m domestic)");
assertEqual(buckets[0].schengen_departure_pax, expectedSch, "A.4) schengen breakdown = SUM(6x5m schengen)");
assertEqual(buckets[0].non_schengen_departure_pax, expectedNonSch, "A.5) non-schengen breakdown = SUM(6x5m non-schengen)");
assertEqual(expectedDom + expectedSch + expectedNonSch, expectedTotal, "A.6) domestic+schengen+non_schengen = total");

// --------------------------------------------------------------------
// B) congestion classification uses THIS airport's OWN percentile
//    distribution, NOT the old fixed wait-minute thresholds (10/15/30)
// --------------------------------------------------------------------
var distribution = [p(0, 0, 100, 0, 0, 100, 1), p(1, 0, 200, 0, 0, 200, 1), p(2, 0, 300, 0, 0, 300, 1), p(3, 0, 400, 0, 0, 400, 1)];
var thresholds = flowCongestionThresholds(distribution);
assertTrue(thresholds.p50 !== 10 && thresholds.p50 !== 15 && thresholds.p50 !== 30, "B.1) thresholds gelmiyor eski wait-minute sabitlerinden (10/15/30) - passenger sayısı skalasında");
assertEqual(congestionRiskForFlow(50, thresholds), "LOW", "B.2) P50 altı -> LOW (Fast)");
assertEqual(congestionRiskForFlow(1000, thresholds), "CRITICAL", "B.3) tüm dağılımın üstü -> CRITICAL (Severe)");

// --------------------------------------------------------------------
// C) annotateFlowPointsWithRisk gives every bucket a risk/risk_label
// --------------------------------------------------------------------
var annotated = annotateFlowPointsWithRisk(to30MinuteFlowBuckets(distribution));
assertTrue(annotated.every(function (b) { return b.risk && b.risk_label; }), "C) her 30dk bucket bir risk/risk_label taşıyor (current status badge için)");

// --------------------------------------------------------------------
// D) peak = max passenger flow (not max risk/wait)
// --------------------------------------------------------------------
var peakPoints = [p(9, 0, 500, 0, 0, 500, 10), p(10, 0, 2840, 0, 0, 2840, 40), p(11, 0, 100, 0, 0, 100, 2)];
var summary = flowPeakBestSummary(peakPoints);
assertEqual(summary.peakPax, 2840, "D.1) peak = en yüksek total_departure_pax");
assertEqual(summary.peakHour, "10a", "D.2) peak saati doğru (10:00 local)");
assertEqual(summary.lowestPax, 100, "D.3) lowest = en düşük SIFIR OLMAYAN total_departure_pax");

// --------------------------------------------------------------------
// E) zero-passenger buckets excluded from "lowest" (not treated as best)
// --------------------------------------------------------------------
var withZero = [p(4, 0, 0, 0, 0, 0, 0), p(5, 0, 300, 0, 0, 300, 5)];
var summaryZero = flowPeakBestSummary(withZero);
assertEqual(summaryZero.lowestPax, 300, "E) sıfır-yolcu bucket 'lowest' olarak seçilmiyor (veri yok, en iyi değil)");

// --------------------------------------------------------------------
// F) local timezone preserved through bucketing (airport-local, not raw UTC)
// --------------------------------------------------------------------
var localTz = to30MinuteFlowBuckets([p(14, 5, 50, 25, 0, 25, 3)]);
assertEqual(hhmm(localTz[0].window_start_local), "14:00", "F) 14:05 local -> 14:00-14:30 bucket (airport local saat korunuyor)");

console.log("");
if (failures > 0) {
  console.log(failures + " test FAILED");
  process.exit(1);
} else {
  console.log("All tests PASSED");
}
