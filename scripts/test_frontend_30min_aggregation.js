// Manuel/ad-hoc Node testi: app/web/static/index.html içindeki 30-dakikalık
// görsel aggregation katmanının davranışını doğrular. Repo'da bir JS test
// framework'ü olmadığı için bu, fonksiyonların index.html'den BİREBİR
// kopyalanmış (yeniden yazılmamış) hâliyle çalıştırılan, çalıştırılabilir
// bir regresyon script'idir. `node scripts/test_frontend_30min_aggregation.js`
// ile çalıştırılır.

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
var WAIT_RISK_LOW_MINUTES = 10, WAIT_RISK_MEDIUM_MINUTES = 15, WAIT_RISK_HIGH_MINUTES = 30;
function riskFromWaitMinutes(wait) {
  if (wait == null) return "UNKNOWN";
  if (wait < WAIT_RISK_LOW_MINUTES) return "LOW";
  if (wait < WAIT_RISK_MEDIUM_MINUTES) return "MEDIUM";
  if (wait < WAIT_RISK_HIGH_MINUTES) return "HIGH";
  return "CRITICAL";
}

var VISUAL_BUCKET_MINUTES = 30;

function to30MinuteVisualBuckets(points) {
  if (!points || !points.length) return points || [];
  var buckets = {};
  var order = [];
  points.forEach(function (p) {
    var rawLocal = p.window_start_local || p.window_start;
    var rawUtc = p.window_start;
    var minute = localMinuteOf(rawLocal);
    if (minute == null) return;
    var bucketMinute = minute < 30 ? 0 : 30;
    var backOffset = -(minute - bucketMinute);
    var bucketIndex = (localHourOf(rawLocal) * 2) + (bucketMinute === 30 ? 1 : 0)
      + "@" + rawLocal.slice(0, 10);
    if (!buckets[bucketIndex]) {
      var startLocal = addMinutesToIsoLocal(rawLocal, backOffset);
      var startUtc = addMinutesToIsoLocal(rawUtc, backOffset);
      buckets[bucketIndex] = {
        window_start: startUtc,
        window_start_local: startLocal,
        window_end: addMinutesToIsoLocal(startUtc, VISUAL_BUCKET_MINUTES),
        window_end_local: addMinutesToIsoLocal(startLocal, VISUAL_BUCKET_MINUTES),
        _sum: 0, _count: 0,
      };
      order.push(bucketIndex);
    }
    var bucket = buckets[bucketIndex];
    if (p.estimated_wait_minutes != null) {
      bucket._sum += p.estimated_wait_minutes;
      bucket._count += 1;
    }
  });
  order.sort(function (a, b) {
    return buckets[a].window_start_local < buckets[b].window_start_local ? -1 : 1;
  });
  return order.map(function (idx) {
    var b = buckets[idx];
    var avg = b._count > 0 ? b._sum / b._count : null;
    var risk = riskFromWaitMinutes(avg);
    return {
      window_start: b.window_start,
      window_start_local: b.window_start_local,
      window_end: b.window_end,
      window_end_local: b.window_end_local,
      estimated_wait_minutes: avg,
      risk: risk,
      risk_label: RISK_UI_LABEL[risk] || RISK_UI_LABEL.UNKNOWN,
    };
  });
}

function formatHour12(hour) {
  var h = ((hour % 24) + 24) % 24;
  var period = h < 12 ? "a" : "p";
  var display = h % 12;
  if (display === 0) display = 12;
  return display + period;
}

var FULL_HOUR_LABELS = {};
for (var _h = 0; _h < 24; _h++) FULL_HOUR_LABELS[_h] = true;

// --------------------------------------------------------------------
// Test helpers
// --------------------------------------------------------------------
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

function makeFullDay5MinPoints(waitFn) {
  // Bir güne ait 288 tane 5dk noktası üretir (00:00..23:55, +03:00 offset).
  var points = [];
  for (var h = 0; h < 24; h++) {
    for (var m = 0; m < 60; m += 5) {
      var hh = h < 10 ? "0" + h : "" + h;
      var mm = m < 10 ? "0" + m : "" + m;
      var iso = "2026-09-26T" + hh + ":" + mm + ":00+03:00";
      points.push({ window_start_local: iso, window_start: iso, estimated_wait_minutes: waitFn(h, m) });
    }
  }
  return points;
}

// --------------------------------------------------------------------
// A) 5-minute source series is not mutated by the aggregation call
// --------------------------------------------------------------------
var source = makeFullDay5MinPoints(function () { return 5.0; });
var sourceCopy = JSON.parse(JSON.stringify(source));
to30MinuteVisualBuckets(source);
assertEqual(source, sourceCopy, "A) 5-minute source series bozulmuyor (aggregation input'u mutasyona uğratmıyor)");

// --------------------------------------------------------------------
// B) 30-minute aggregation count = 48
// --------------------------------------------------------------------
var buckets = to30MinuteVisualBuckets(source);
assertEqual(buckets.length, 48, "B) 30-dakikalık aggregation count = 48");

// --------------------------------------------------------------------
// C) 12:00-12:30 bucket = 12:00..12:25 arasındaki 6 point'in average'ı
// --------------------------------------------------------------------
var variedWaits = { "12:00": 2, "12:05": 4, "12:10": 6, "12:15": 8, "12:20": 10, "12:25": 12 };
var variedSource = makeFullDay5MinPoints(function (h, m) {
  var hh = h < 10 ? "0" + h : "" + h;
  var mm = m < 10 ? "0" + m : "" + m;
  var key = hh + ":" + mm;
  return variedWaits[key] != null ? variedWaits[key] : 0;
});
var variedBuckets = to30MinuteVisualBuckets(variedSource);
var bucket1200 = variedBuckets.filter(function (b) { return hhmm(b.window_start_local) === "12:00"; })[0];
var expectedAvg = (2 + 4 + 6 + 8 + 10 + 12) / 6; // = 7
assertEqual(bucket1200.estimated_wait_minutes, expectedAvg, "C) 12:00-12:30 bucket = 12:00..12:25 noktalarının ortalaması (7.0)");
assertEqual(hhmm(bucket1200.window_end_local), "12:30", "C.2) 12:00 bucket'ın window_end_local'ı 12:30");

// --------------------------------------------------------------------
// D) 13:30 bucket altında hour label YOK / E) 14:00 bucket altında label "2p"
// --------------------------------------------------------------------
function labelFor(bucket) {
  var hour = localHourOf(bucket.window_start_local);
  var minute = localMinuteOf(bucket.window_start_local);
  var isHourMark = minute === 0;
  return (hour != null && isHourMark && FULL_HOUR_LABELS[hour]) ? formatHour12(hour) : "";
}
var b1330 = buckets.filter(function (b) { return hhmm(b.window_start_local) === "13:30"; })[0];
var b1400 = buckets.filter(function (b) { return hhmm(b.window_start_local) === "14:00"; })[0];
assertEqual(labelFor(b1330), "", "D) 13:30 bucket altında hour label YOK");
assertEqual(labelFor(b1400), "2p", "E) 14:00 bucket altında label '2p'");

// --------------------------------------------------------------------
// F) Current bucket detection: 21:17 -> 21:00-21:30 bucket'ı current
// --------------------------------------------------------------------
function pickCurrentBucket(buckets, nowMs) {
  var chosen = null;
  for (var i = 0; i < buckets.length; i++) {
    var ms = new Date(buckets[i].window_start_local).getTime();
    if (!isNaN(ms) && ms <= nowMs) chosen = buckets[i];
  }
  return chosen || buckets[0];
}
var now2117 = new Date("2026-09-26T21:17:00+03:00").getTime();
var current = pickCurrentBucket(buckets, now2117);
assertEqual(hhmm(current.window_start_local), "21:00", "F) 21:17 anında current bucket = 21:00-21:30");
assertEqual(hhmm(current.window_end_local), "21:30", "F.2) current bucket'ın end'i 21:30");

// --------------------------------------------------------------------
// Day-rollover: 23:30 bucket'ın window_end_local'ı ertesi güne geçmeli
// --------------------------------------------------------------------
var b2330 = buckets.filter(function (b) { return hhmm(b.window_start_local) === "23:30"; })[0];
assertEqual(b2330.window_end_local, "2026-09-27T00:00:00+03:00", "G) 23:30 bucket'ın end'i ertesi gün 00:00'a doğru taşıyor");

console.log("");
if (failures > 0) {
  console.log(failures + " test FAILED");
  process.exit(1);
} else {
  console.log("All tests PASSED");
}
