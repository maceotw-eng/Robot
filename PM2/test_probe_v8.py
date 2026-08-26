#!/usr/bin/env python3
"""
Offline check of the probe harvester against a mock Gamma that reproduces every
fault found so far, each of which only became visible at a different scale:

  v1  short-page stop      - a page shorter than the requested limit ended the
                             harvest on page one (Gamma caps ~100 rows)
  v1  unbounded window     - ascending endDate with no date bound walked a
                             pre-2023 legacy stratum
  v2  deep-offset wall     - offset<=2000 succeeds, offset>=2100 returns 422
  v3  same-day cross-batch - a single endDate spanning a batch boundary must
                             come through exactly once, not twice, not zero
  v3  undersized cursor    - one day deeper than the offset ceiling must raise
                             DepthWall, never loop or silently truncate
  v3  span wall            - CLOB rejects wide startTs/endTs spans ("interval
                             is too long"); the fetch must fall back to
                             interval=max and slice client-side
  v3  silent truncation    - a harvest that exhausts its page budget must
                             declare itself incomplete in HARVEST_META
"""
import json
import os
import sys
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import polymarket_probe as p  # noqa: E402

FAILURES = []
PAGE_CAP = 100        # Gamma's real per-page cap regardless of the limit asked
OFFSET_WALL = 2000    # measured: 2000 ok, 2100 -> 422


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


def make_market(i, day, legacy=False, payout="clean"):
    tok = ([str(10 ** 76 + i), str(10 ** 76 + i + 1)] if not legacy
           else [str(1000 + i), str(1001 + i)])
    prices = {"clean": ["1", "0"] if i % 2 else ["0", "1"],
              "near": ["0.99", "0.01"],
              "zero": ["0", "0"],
              "missing": None}[payout]
    m = {"id": i, "question": "Q%d" % i, "closed": True,
         "endDate": "%sT00:00:00Z" % day,
         "volumeNum": 25000, "outcomes": json.dumps(["Yes", "No"]),
         "clobTokenIds": json.dumps(tok), "resolutionSource": "src"}
    if prices is not None:
        m["outcomePrices"] = json.dumps(prices)
    return m


def build_universe(same_day_block=0, mega_day=None, mega_count=0):
    """Legacy stratum + an in-window population, optionally with pathologies."""
    uni = [make_market(i, "%d-06-15" % (2011 + (i % 3)), legacy=True)
           for i in range(40)]
    # ~2600 in-window rows: deeper than the 1900 offset ceiling, so the cursor
    # is forced to advance at least once. Spread over distinct days.
    idx = 100
    for d in range(26):
        day = "2023-01-%02d" % (d + 1) if d < 26 else None
        for k in range(100):
            payout = ("near" if (idx % 25 == 0) else
                      "zero" if (idx % 37 == 0) else "clean")
            uni.append(make_market(idx, day, payout=payout))
            idx += 1
    # A block of rows sharing ONE endDate, straddling a page boundary.
    for k in range(same_day_block):
        uni.append(make_market(idx, "2024-03-03"))
        idx += 1
    # A day deeper than the offset ceiling: the cursor cannot get past it.
    for k in range(mega_count):
        uni.append(make_market(idx, mega_day))
        idx += 1
    return uni


def make_get(universe, wall=OFFSET_WALL):
    def fake_get(url, params=None, retries=4, timeout=30):
        params = params or {}
        if "/markets" not in url:
            return {"history": []}
        off = int(params.get("offset", 0))
        if off > wall:
            raise urllib.error.HTTPError(url, 422, "Unprocessable", {}, None)
        rows = list(universe)
        lo, hi = params.get("end_date_min"), params.get("end_date_max")
        if lo and hi and lo == hi:
            # measured real behaviour: zero-length range -> 422
            raise urllib.error.HTTPError(url, 422, "invalid time range", {}, None)

        def _full(ts, end=False):
            if ts is None or "T" in ts:
                return ts
            return ts + ("T23:59:59Z" if end else "T00:00:00Z")
        lo_f, hi_f = _full(lo), _full(hi, end=True)
        if lo_f:
            rows = [m for m in rows if m["endDate"] >= lo_f]
        if hi_f:
            rows = [m for m in rows if m["endDate"] <= hi_f]
        asc = str(params.get("ascending", "true")).lower() != "false"
        if "volume_num_min" in params:
            fl = float(params["volume_num_min"])
            rows = [m for m in rows if float(m.get("volumeNum", 0)) >= fl]
        if params.get("order") == "id":
            rows.sort(key=lambda m: m["id"], reverse=not asc)
        else:
            rows.sort(key=lambda m: (m["endDate"], m["id"]),
                      reverse=not asc)
        limit = min(int(params.get("limit", 100)), PAGE_CAP)
        return rows[off:off + limit]
    return fake_get


print("=" * 70)
print("probe harvester offline check")
print("=" * 70)

# ---------------------------------------------------------------- baseline
UNI = build_universe()
p.get = make_get(UNI)
in_window = [m for m in UNI if m["endDate"][:10] >= "2023-01-01"]

print("\n[1] deep-offset wall (v2's HTTP 422 on page 22)")
wall_hit = False
try:
    make_get(UNI)("https://x/markets", {"offset": 2100})
except urllib.error.HTTPError as exc:
    wall_hit = (exc.code == 422)
check("mock reproduces the wall: offset 2100 -> 422", wall_hit)
check("mock allows offset 2000 (matches the measured boundary)",
      isinstance(make_get(UNI)("https://x/markets", {"offset": 2000}), list))
got = p.harvest_markets(max_pages=400, page_size=500,
                        date_min="2023-01-01", date_max="2026-06-30")
check("harvest completes without hitting 422",
      len(got) > OFFSET_WALL, "got %d" % len(got))
check("population is deeper than the offset ceiling, so the cursor had to move",
      len(in_window) > p.OFFSET_CEILING, "in_window=%d" % len(in_window))
check("every in-window market harvested exactly once",
      len(got) == len(in_window), "got %d want %d" % (len(got), len(in_window)))
check("no duplicate ids", len({m["id"] for m in got}) == len(got))

print("\n[2] window still excludes the legacy stratum")
check("no pre-2023 rows", all(m["endDate"][:4] >= "2023" for m in got))

print("\n[3] same-day block straddling a batch boundary")
UNI2 = build_universe(same_day_block=250)
p.get = make_get(UNI2)
want2 = [m for m in UNI2 if m["endDate"][:10] >= "2023-01-01"]
got2 = p.harvest_markets(max_pages=400, page_size=500,
                         date_min="2023-01-01", date_max="2026-06-30")
same_day = [m for m in got2 if m["endDate"][:10] == "2024-03-03"]
check("all rows sharing one endDate are harvested", len(same_day) == 250,
      "got %d" % len(same_day))
check("boundary-day overlap does not duplicate",
      len({m["id"] for m in got2}) == len(got2))
check("total still exact", len(got2) == len(want2),
      "got %d want %d" % (len(got2), len(want2)))

print("\n[4] dense days: dual scan succeeds up to ~2x ceiling, then DepthWall")
# 2400 rows on one day: deeper than one pass (ceiling ~2000) but within two.
UNI3 = build_universe(mega_day="2025-05-05", mega_count=2400)
p.get = make_get(UNI3)
got3 = p.harvest_markets(max_pages=400, page_size=500,
                         date_min="2025-05-05", date_max="2026-06-30")
mega = [m for m in got3 if m["endDate"][:10] == "2025-05-05"]
check("a 2400-row day is fully harvested by the two-direction scan",
      len(mega) == 2400, "got %d" % len(mega))
check("no duplicates from the overlapping directions",
      len({m["id"] for m in got3}) == len(got3))
check("meta records the dual-scanned dense day",
      p.HARVEST_META.get("dense_days_dual_scanned", 0) >= 1)

# 4500 rows on one day: beyond both passes -> must fail loudly.
UNI4 = build_universe(mega_day="2025-06-06", mega_count=4500)
p.get = make_get(UNI4)
raised = None
try:
    p.harvest_markets(max_pages=800, page_size=500,
                      date_min="2025-06-06", date_max="2026-06-30")
except p.DepthWall as exc:
    raised = str(exc)
check("DepthWall raised rather than looping or truncating silently",
      raised is not None, "no exception")
check("message names the blocking day", raised and "2025-06-06" in raised,
      "msg=%s" % raised)

print("\n[4b] continuous dense days (the real post-2025-11 regime)")
# Three consecutive days x 2500 rows each: every day needs the dual scan.
UNI5 = build_universe()
idx5 = 900000
for d in ("2025-11-08", "2025-11-09", "2025-11-10"):
    for k in range(2500):
        UNI5.append(make_market(idx5, d))
        idx5 += 1
p.get = make_get(UNI5)
got5 = p.harvest_markets(max_pages=2000, page_size=500,
                         date_min="2025-11-08", date_max="2025-11-10")
check("all three dense days fully harvested",
      len(got5) == 7500, "got %d" % len(got5))
check("no duplicates across days", len({m["id"] for m in got5}) == len(got5))
check("meta shows three dual-scanned days",
      p.HARVEST_META.get("dense_days_dual_scanned") == 3,
      "meta=%s" % p.HARVEST_META)

print("\n[4c] self-verifying server-side volume floor")
# A day of 3000 rows where 2600 are tiny hourly markets: with the verified
# server floor the day fits in a single pass; without it, dual scan territory.
UNI6 = build_universe()
idx6 = 950000
for k in range(3000):
    m = make_market(idx6, "2026-02-02")
    m["volumeNum"] = 500 if k < 2600 else 25000     # mostly sub-floor
    UNI6.append(m)
    idx6 += 1
p.get = make_get(UNI6)
ok, note = p._server_floor_verified("2026-02-02", 10000.0)
check("filter verified against a mock that honours it", ok, note)
got6 = p.harvest_markets(max_pages=2000, page_size=500,
                         date_min="2026-02-02", date_max="2026-02-02",
                         server_floor=10000.0)
check("dense day collapses under the server floor (single pass, no dual scan)",
      p.HARVEST_META.get("dense_days_dual_scanned") == 0 and
      p.HARVEST_META.get("server_volume_floor_used") is True,
      "meta=%s" % p.HARVEST_META)
check("harvest holds exactly the above-floor rows",
      len([m for m in got6 if m["endDate"][:10] == "2026-02-02"]) == 400,
      "got %d" % len(got6))


def ignore_filter_get(universe):
    real = make_get(universe)
    def fake(url, params=None, retries=4, timeout=30):
        params = dict(params or {})
        params.pop("volume_num_min", None)          # Gamma-style silent ignore
        return real(url, params)
    return fake


p.get = ignore_filter_get(UNI6)
ok2, note2 = p._server_floor_verified("2026-02-02", 10000.0)
check("a silently-ignored filter is detected and NOT trusted", not ok2, note2)

p.get = make_get(UNI)  # restore

print("\n[5] censuses still correct on the harvested population")
p.get = make_get(UNI)
uni, eligible = p.summarise_universe(got)
near = sum(1 for m in got if json.loads(m.get("outcomePrices", "[]")) == ["0.99", "0.01"])
zero = sum(1 for m in got if json.loads(m.get("outcomePrices", "[]")) == ["0", "0"])
pay = uni["settlement_payout_census"]
check("near-certain 0.99/0.01 NOT counted as settled",
      pay.get("unsettled_prices", 0) == near and near > 0,
      "unsettled=%s near=%d" % (pay.get("unsettled_prices"), near))
check("all-zero payouts counted separately", pay.get("all_zero", 0) == zero and zero > 0)
check("clean 1/0 payouts are the settled population",
      pay.get("settled_payout", 0) == len(got) - near - zero)
check("token census: in-window rows all usable",
      uni["token_mapping_census"].get("ok", 0) == len(got))
legacy_uni, _ = p.summarise_universe(UNI)
check("legacy short ids counted, not dropped",
      legacy_uni["token_mapping_census"].get("legacy_short_id") == 40)
check("exact payout returns the winning index",
      p.settlement_payout({"outcomePrices": json.dumps(["1", "0"])})
      == (0, "settled_payout"))
check("0.999 rejected as a settlement",
      p.settlement_payout({"outcomePrices": json.dumps(["0.999", "0.001"])})[1]
      == "unsettled_prices")
print("       -> %s" % uni["R02_polymarket_path"])

print("\n[6] page budget exhaustion is reported, not silently partial")
p.get = make_get(UNI)
short = p.harvest_markets(max_pages=3, page_size=500,
                          date_min="2023-01-01", date_max="2026-06-30")
check("truncated harvest returns fewer rows", len(short) < len(in_window),
      "got %d" % len(short))
check("HARVEST_META declares the truncation",
      p.HARVEST_META.get("truncated") is True, "meta=%s" % p.HARVEST_META)
full = p.harvest_markets(max_pages=400, page_size=500,
                         date_min="2023-01-01", date_max="2026-06-30")
check("a complete harvest declares itself complete",
      p.HARVEST_META.get("truncated") is False and
      p.HARVEST_META.get("markets_harvested") == len(full),
      "meta=%s" % p.HARVEST_META)

print("\n[7] prices-history span wall: window -> interval=max fallback")
DAY_S = 86400
BASE = 1700000000
SERIES = [{"t": BASE + i * DAY_S, "p": 0.5} for i in range(100)]
clob_calls = []


def make_clob_get(reject_window=True, empty_window=False):
    def fake(url, params=None, retries=4, timeout=30):
        params = params or {}
        if "/markets" in url:
            return []
        clob_calls.append(dict(params))
        if "startTs" in params:
            if reject_window:
                raise p.ApiError(400, "invalid filters: 'startTs' and 'endTs' "
                                      "interval is too long", url)
            if empty_window:
                return {"history": []}
        return {"history": list(SERIES)}
    return fake


end_ms = (BASE + 90 * DAY_S) * 1000

p.get = make_clob_get(reject_window=True)
clob_calls.clear()
hist, err, mode = p.price_history("tok", 1440, end_ms)
check("window attempted first", "startTs" in clob_calls[0])
check("span rejection falls back to interval=max", mode == "interval_max",
      "mode=%s err=%s" % (mode, err))
check("fallback slices to the requested window client-side",
      hist and all((end_ms - 36 * DAY_S * 1000) <= h["t"] * 1000 for h in hist)
      and len(hist) < len(SERIES), "points=%s" % (len(hist) if hist else None))
check("no error surfaced when the fallback succeeds", err is None)

p.get = make_clob_get(reject_window=False, empty_window=True)
hist2, err2, mode2 = p.price_history("tok", 1440, end_ms)
check("an empty window also consults interval=max",
      mode2 == "interval_max" and hist2, "mode=%s" % mode2)

check("ApiError carries the API's own explanation",
      "interval is too long" in str(p.ApiError(400, "invalid filters: "
      "'startTs' and 'endTs' interval is too long")))

p.get = make_get(UNI)  # restore for anything after

print("\n[8] streaming state + resume: a killed run continues, not restarts")
import tempfile, shutil
tmpd = tempfile.mkdtemp()
p.get = make_get(UNI)
# first run: tiny budget -> dies mid-flight (truncated)
r1 = p.harvest_markets(max_pages=4, page_size=500,
                       date_min="2023-01-01", date_max="2026-06-30",
                       state_dir=tmpd)
pages1 = p.HARVEST_META["pages_used"]
check("first run truncated as expected", p.HARVEST_META["truncated"] is True)
# second run: full budget, same state dir -> resumes
r2 = p.harvest_markets(max_pages=400, page_size=500,
                       date_min="2023-01-01", date_max="2026-06-30",
                       state_dir=tmpd)
pages2 = p.HARVEST_META["pages_used"]
check("resume declared in meta", p.HARVEST_META.get("resumed_from_state") in (True, False))
check("resumed harvest is complete and exact",
      len(r2) == len(in_window) and
      {m["id"] for m in r2} == {m["id"] for m in in_window},
      "got %d want %d" % (len(r2), len(in_window)))
# fresh full run for page-count comparison
shutil.rmtree(tmpd); tmpd2 = tempfile.mkdtemp()
r3 = p.harvest_markets(max_pages=400, page_size=500,
                       date_min="2023-01-01", date_max="2026-06-30",
                       state_dir=tmpd2)
pages3 = p.HARVEST_META["pages_used"]
check("rows slimmed on ingestion (no thumbnails in memory)",
      all("thumbnailPaths" not in m for m in r3[:50]))
shutil.rmtree(tmpd2)
check("resume did not redo the whole harvest",
      pages2 <= pages3, "resume=%d fresh=%d" % (pages2, pages3))

print("\n" + "=" * 70)
print("FAILED: %s" % FAILURES if FAILURES else "ALL CHECKS PASSED")
sys.exit(1 if FAILURES else 0)
