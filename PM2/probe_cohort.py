#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
PM-2 supplement: COHORT-CONDITIONAL checkpoint absence.

v8 measured T=7 / T=30 absence over ALL sampled markets and both windows
tripped the 30% downgrade flag. The absent_breakdown showed why: most of the
absence is markets BORN AFTER the checkpoint (median nearest-point gap at
T=30 was ~717h ~= the market's own birth near resolution), not missing data.

A market that lived 3 days was never a valid subject for a T=30 question.
The honest fix is a cohort definition - "eligible for window T = lifespan
covers the checkpoint (startDate <= endDate - T days)" - and absence measured
WITHIN that cohort. This script does exactly that, reusing v8's fetch layer.

Run on the Tokyo box next to harvest_stream.ndjson (probe_state/):
    python3 probe_cohort.py
Writes cohort_result.json. ~200 CLOB calls, ~10 minutes.
"""

import json
import random
import sys

sys.path.insert(0, ".")
import polymarket_probe as p   # v8 on disk as polymarket_probe.py

STREAM = "probe_state/harvest_stream.ndjson"
VOLUME_FLOOR = 10000.0
PER_WINDOW = 100
SEED = 20260826               # recorded: sampling is reproducible

PLAN = {7: (60, 14), 30: (1440, 35)}   # T: (fidelity, window_days)


def load_markets():
    rows = []
    with open(STREAM) as fh:
        for line in fh:
            try:
                rows.append(json.loads(line))
            except Exception:
                continue
    return rows


def eligible_binary(m):
    vol, _ = p.volume_of(m)
    if vol < VOLUME_FLOOR:
        return False
    toks = p.parse_token_ids(m)
    if not toks or len(toks) != 2:
        return False
    if not m.get("endDate") or not m.get("startDate"):
        return False
    return True


def main():
    rows = load_markets()
    pool = [m for m in rows if eligible_binary(m)]
    print("stream rows: %d | eligible binary with dates: %d"
          % (len(rows), len(pool)))

    rng = random.Random(SEED)
    out = {"seed": SEED, "per_window": PER_WINDOW, "windows": {}}

    for T, (fid, days) in PLAN.items():
        cohort = []
        for m in pool:
            end_ms = p.parse_dt(m["endDate"])
            start_ms = p.parse_dt(m["startDate"])
            if not end_ms or not start_ms:
                continue
            if end_ms - start_ms >= (T + 1) * p.DAY_MS:
                cohort.append(m)
        frac = len(cohort) / max(1, len(pool))
        print("[T=%d] cohort (lifespan >= %dd): %d markets (%.1f%% of pool)"
              % (T, T + 1, len(cohort), 100 * frac))

        sample = rng.sample(cohort, min(PER_WINDOW, len(cohort)))
        present = absent_nohist = absent_nopoint = errors = 0
        gaps = []
        for i, m in enumerate(sample):
            end_ms = p.parse_dt(m["endDate"])
            hist, err, _mode = p.price_history(
                p.parse_token_ids(m)[0], fid, end_ms, window_days=days)
            if err:
                errors += 1
                continue
            if not hist:
                absent_nohist += 1
                continue
            stamps = [p._norm_ts_ms(pt["t"]) for pt in hist
                      if pt.get("t") is not None]
            target = end_ms - T * p.DAY_MS
            hit = [s for s in stamps if target <= s < target + p.DAY_MS]
            if hit:
                present += 1
            else:
                absent_nopoint += 1
            if stamps:
                near = min(stamps, key=lambda s: abs(s - target))
                gaps.append(abs(near - target) / 3600000.0)
            sys.stderr.write("\r  T=%d %d/%d" % (T, i + 1, len(sample)))
        sys.stderr.write("\n")

        n = present + absent_nohist + absent_nopoint
        absence = (absent_nohist + absent_nopoint) / max(1, n)
        gaps.sort()
        med = gaps[len(gaps) // 2] if gaps else None
        rec = {"cohort_size": len(cohort),
               "cohort_fraction_of_pool": round(frac, 4),
               "sampled": len(sample), "scored": n,
               "present": present,
               "absent_no_history": absent_nohist,
               "absent_no_point_in_window": absent_nopoint,
               "fetch_errors": errors,
               "cohort_absence_rate": round(absence, 4),
               "median_gap_hours": round(med, 1) if med is not None else None,
               "EXCEEDS_30PCT": absence > 0.30}
        out["windows"]["T=%d" % T] = rec
        print(json.dumps(rec, indent=2))

    with open("cohort_result.json", "w") as fh:
        json.dump(out, fh, indent=2)
    print("\nwrote cohort_result.json")


if __name__ == "__main__":
    main()
