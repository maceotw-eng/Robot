#!/usr/bin/env python3
"""
Offline check of probe v2 against a mock Gamma that reproduces the exact
faults v1's own output exposed:
  - the API caps a page at 100 rows even when limit=500 is requested
  - unbounded endDate ordering walks a pre-2023 legacy stratum
  - some markets carry legacy short integer ids instead of 77-digit CLOB tokens
  - some closed markets show near-certain prices rather than a 1/0 payout
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import polymarket_probe as p  # noqa: E402

FAILURES = []
PAGE_CAP = 100  # Gamma's real behaviour, regardless of the limit asked for


def check(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name + ("" if cond else "  " + detail))
    if not cond:
        FAILURES.append(name)


def make_market(i, year, legacy=False, payout="clean"):
    tok = ([str(10 ** 76 + i), str(10 ** 76 + i + 1)] if not legacy
           else [str(1000 + i), str(1001 + i)])
    prices = {"clean": ["1", "0"] if i % 2 else ["0", "1"],
              "near": ["0.99", "0.01"],
              "zero": ["0", "0"],
              "missing": None}[payout]
    m = {"id": i, "question": "Q%d" % i, "closed": True,
         "endDate": "%d-06-15T00:00:00Z" % year,
         "volumeNum": 25000, "outcomes": json.dumps(["Yes", "No"]),
         "clobTokenIds": json.dumps(tok), "resolutionSource": "src"}
    if prices is not None:
        m["outcomePrices"] = json.dumps(prices)
    return m


UNIVERSE = []
for i in range(40):
    UNIVERSE.append(make_market(i, 2011 + (i % 3), legacy=True))       # legacy stratum
for i in range(100, 340):
    year = 2023 + ((i - 100) // 80)
    payout = "near" if i % 25 == 0 else ("zero" if i % 37 == 0 else "clean")
    UNIVERSE.append(make_market(i, year, payout=payout))


def fake_get(url, params=None, retries=4, timeout=30):
    params = params or {}
    if "/markets" in url:
        rows = list(UNIVERSE)
        lo, hi = params.get("end_date_min"), params.get("end_date_max")
        if lo:
            rows = [m for m in rows if m["endDate"][:10] >= lo]
        if hi:
            rows = [m for m in rows if m["endDate"][:10] <= hi]
        rows.sort(key=lambda m: m["endDate"])
        off = int(params.get("offset", 0))
        limit = min(int(params.get("limit", 100)), PAGE_CAP)   # the real cap
        return rows[off:off + limit]
    return {"history": []}


p.get = fake_get

print("=" * 66)
print("probe v2 offline check")
print("=" * 66)

print("\n[1] pagination no longer stops on a short page")
got = p.harvest_markets(max_pages=60, page_size=500,
                        date_min="2023-01-01", date_max="2026-06-30")
in_window = [m for m in UNIVERSE if m["endDate"][:10] >= "2023-01-01"]
check("harvest returns the whole window, not just page one",
      len(got) == len(in_window), "got %d want %d" % (len(got), len(in_window)))
check("v1 would have stopped at the page cap", PAGE_CAP < len(in_window))
check("no duplicate ids across pages",
      len({m["id"] for m in got}) == len(got))

print("\n[2] date window keeps the legacy stratum out")
years = sorted({m["endDate"][:4] for m in got})
check("no pre-2023 markets in the harvest", all(y >= "2023" for y in years),
      "years=%s" % years)
check("v1's 2011-2021 stratum is excluded",
      not any(m["endDate"][:4] in ("2011", "2012", "2013") for m in got))

print("\n[3] A-1 mapping census counts failures instead of dropping them")
uni, eligible = p.summarise_universe(got)
census = uni["token_mapping_census"]
check("all in-window markets have usable 77-digit tokens",
      census.get("ok", 0) == len(got), "census=%s" % census)
legacy_uni, _ = p.summarise_universe(UNIVERSE)
check("legacy short ids are counted as legacy_short_id",
      legacy_uni["token_mapping_census"].get("legacy_short_id") == 40,
      "census=%s" % legacy_uni["token_mapping_census"])
check("failure rate is reported",
      legacy_uni["token_mapping_failure_rate"] > 0)

print("\n[4] R-02 settlement census uses an EXACT 1/0 payout test")
pay = uni["settlement_payout_census"]
near = sum(1 for m in got if json.loads(m.get("outcomePrices", "[]")) == ["0.99", "0.01"])
zero = sum(1 for m in got if json.loads(m.get("outcomePrices", "[]")) == ["0", "0"])
check("near-certain 0.99/0.01 is NOT counted as settled",
      pay.get("unsettled_prices", 0) == near and near > 0,
      "unsettled=%s near=%d" % (pay.get("unsettled_prices"), near))
check("all-zero payouts counted separately",
      pay.get("all_zero", 0) == zero and zero > 0)
check("clean 1/0 payouts are the settled population",
      pay.get("settled_payout", 0) == len(got) - near - zero)
check("path recommendation emitted", "R02_polymarket_path" in uni)
print("       -> %s" % uni["R02_polymarket_path"])

single = p.settlement_payout({"outcomePrices": json.dumps(["1", "0"])})
check("exact payout returns the winning index", single == (0, "settled_payout"))
check("0.999 is rejected as a settlement",
      p.settlement_payout({"outcomePrices": json.dumps(["0.999", "0.001"])})[1]
      == "unsettled_prices")
check("missing outcomePrices flagged",
      p.settlement_payout({})[1] == "missing")

print("\n[5] year-stratified sampling covers every slice")
buckets = {}
for m in eligible:
    buckets.setdefault(m["endDate"][:4], []).append(m)
check("eligible spans all in-window years", len(buckets) >= 3,
      "years=%s" % sorted(buckets))
check("eligible excludes unusable-token markets",
      all(p.token_status(m)[1] == "ok" for m in eligible))

print("\n" + "=" * 66)
print("FAILED: %s" % FAILURES if FAILURES else "ALL CHECKS PASSED")
sys.exit(1 if FAILURES else 0)
