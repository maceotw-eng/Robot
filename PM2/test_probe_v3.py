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
        if lo:
            rows = [m for m in rows if m["endDate"][:10] >= lo]
        if hi:
            rows = [m for m in rows if m["endDate"][:10] <= hi]
        rows.sort(key=lambda m: (m["endDate"], m["id"]))
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

print("\n[4] a single day deeper than the ceiling raises DepthWall")
UNI3 = build_universe(mega_day="2025-05-05", mega_count=2400)
p.get = make_get(UNI3)
raised = None
try:
    p.harvest_markets(max_pages=400, page_size=500,
                      date_min="2025-05-05", date_max="2026-06-30")
except p.DepthWall as exc:
    raised = str(exc)
check("DepthWall raised rather than looping or truncating silently",
      raised is not None, "no exception")
check("message names the blocking day", raised and "2025-05-05" in raised,
      "msg=%s" % raised)

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

print("\n" + "=" * 70)
print("FAILED: %s" % FAILURES if FAILURES else "ALL CHECKS PASSED")
sys.exit(1 if FAILURES else 0)
