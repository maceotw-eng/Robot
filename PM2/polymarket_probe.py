#!/usr/bin/env python3
"""
PM-2 B-line empirical gate — settles the four facts the charter freeze depends on.

Run this on the VPS (open egress, no TLS interception). Stdlib only, no auth.
It answers, with numbers rather than assumption:

  G1  What is the ACTUAL earliest/latest resolved-market date Gamma will serve?
      (charter 2 assumes 2023-01-01 ~ 2026-06-30)
  G2  How many resolved binary markets clear the $10,000 volume threshold,
      broken down by year? (charter 5 needs >= 100 markets per bucket cell)
  G3  What is the granularity floor of /prices-history for RESOLVED markets?
      (Polymarket issue #216: the 2024 US Presidential market — the most liquid
       market on the venue — returns {'history': []} at fidelity=60 but has data
       at fidelity=720. If that is systemic, charter 3's T=1 window is not
       measurable as defined.)
  G4  What is the ACTUAL absence rate for the T in {1, 7, 30} checkpoints?
      (charter 3 says absent = counted, not interpolated; >30% forces a
       downgrade note on that window)

Writes probe_result.json plus a human-readable summary on stdout.

    python3 polymarket_probe.py                 # default: 150-market sample
    python3 polymarket_probe.py --sample 400    # tighter confidence intervals
"""

import argparse
import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
DAY_MS = 86400000
CHECKPOINTS = [1, 7, 30]           # charter 3, T in days before resolution
FIDELITIES = [1, 10, 60, 360, 720, 1440]   # minutes
VOLUME_FLOOR = 10000.0             # charter 2

UA = {"User-Agent": "pm2-probe/1.0", "Accept": "application/json"}


def get(url, params=None, retries=4, timeout=30):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                time.sleep(2 ** attempt)
                last = exc
                continue
            raise
        except (urllib.error.URLError, ssl.SSLError, OSError, TimeoutError) as exc:
            time.sleep(1.5 * (attempt + 1))
            last = exc
    raise RuntimeError("giving up on %s: %s" % (url, last))


def iso(ms):
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000.0, timezone.utc).strftime("%Y-%m-%d")


def parse_dt(value):
    """Gamma serves ISO-8601 strings; return epoch ms."""
    if not value:
        return None
    txt = str(value).replace("Z", "+00:00")
    try:
        return int(datetime.fromisoformat(txt).timestamp() * 1000)
    except ValueError:
        return None


def parse_token_ids(market):
    """
    Gamma returns clobTokenIds as a JSON-ENCODED STRING, not a list — the classic
    Gamma/CLOB bridging trap. Handle both shapes. Element 0 = YES, 1 = NO.
    """
    raw = market.get("clobTokenIds")
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, list) else []
        except json.JSONDecodeError:
            return []
    return []


def volume_of(market):
    """Gamma exposes several volume fields; report which one we used."""
    for key in ("volumeNum", "volume", "volumeClob"):
        val = market.get(key)
        if val not in (None, ""):
            try:
                return float(val), key
            except (TypeError, ValueError):
                continue
    return 0.0, None


# ------------------------------------------------------------------ G1 / G2

def harvest_markets(max_pages, page_size=500):
    """Paginate Gamma for closed markets, ascending by endDate."""
    markets, offset = [], 0
    for page in range(max_pages):
        batch = get(GAMMA + "/markets", {
            "closed": "true", "limit": page_size, "offset": offset,
            "order": "endDate", "ascending": "true",
        })
        if not isinstance(batch, list) or not batch:
            break
        markets.extend(batch)
        sys.stderr.write("\r  gamma page %d -> %d markets" % (page + 1, len(markets)))
        sys.stderr.flush()
        if len(batch) < page_size:
            break
        offset += len(batch)
    sys.stderr.write("\n")
    return markets


def field_census(markets, sample_values=4):
    """
    Enumerate what Gamma ACTUALLY returns, rather than what we assume.

    Rationale: the Binance side of this project shipped with an assumed status
    value space (ACTIVE/OPEN/TRADING) that turned out to be wrong (the real
    values are REGISTERED/CLOSED), and the first production run filtered every
    single market out. Do not repeat that here - measure the schema, then write
    the charter field names from the measurement.
    """
    presence = Counter()
    types = defaultdict(Counter)
    samples = defaultdict(list)
    for market in markets:
        if not isinstance(market, dict):
            continue
        for key, val in market.items():
            if val in (None, "", [], {}):
                continue
            presence[key] += 1
            types[key][type(val).__name__] += 1
            bucket = samples[key]
            if len(bucket) < sample_values:
                text = str(val)
                if text not in bucket:
                    bucket.append(text[:90])

    total = max(1, len(markets))
    census = {}
    for key, count in presence.most_common():
        census[key] = {
            "present_pct": round(100.0 * count / total, 1),
            "types": dict(types[key]),
            "samples": samples[key],
        }

    # Anything that smells like a status / resolution field gets called out,
    # because those are the two the charter has to name explicitly.
    interesting = {k: v for k, v in census.items()
                   if any(w in k.lower() for w in
                          ("status", "resolv", "resolution", "closed", "void",
                           "outcome", "volume", "liquidit", "accepting", "archiv"))}
    return {"total_markets_scanned": len(markets),
            "field_count": len(census),
            "status_and_resolution_fields": interesting,
            "all_fields": census}


def value_space(markets, keys):
    """Full observed value distribution for the fields the charter must name."""
    out = {}
    for key in keys:
        counter = Counter()
        for market in markets:
            if isinstance(market, dict) and key in market:
                counter[str(market.get(key))] += 1
        if counter:
            out[key] = dict(counter.most_common(20))
    return out


def summarise_universe(markets):
    vol_fields = Counter()
    by_year = defaultdict(lambda: {"all": 0, "above_floor": 0, "binary": 0})
    dates, eligible = [], []
    resolution_fields = Counter()
    voided = 0

    for m in markets:
        end_ms = parse_dt(m.get("endDate"))
        if end_ms:
            dates.append(end_ms)
        year = iso(end_ms)[:4] if end_ms else "unknown"

        vol, field = volume_of(m)
        if field:
            vol_fields[field] += 1

        outcomes = m.get("outcomes")
        if isinstance(outcomes, str):
            try:
                outcomes = json.loads(outcomes)
            except json.JSONDecodeError:
                outcomes = None
        is_binary = isinstance(outcomes, list) and len(outcomes) == 2

        for key in ("umaResolutionStatus", "resolutionSource", "resolvedBy"):
            if m.get(key):
                resolution_fields[key] += 1
        status = str(m.get("umaResolutionStatus") or "").lower()
        if "void" in status or "invalid" in status:
            voided += 1

        by_year[year]["all"] += 1
        if is_binary:
            by_year[year]["binary"] += 1
        if vol >= VOLUME_FLOOR:
            by_year[year]["above_floor"] += 1
            if is_binary and parse_token_ids(m):
                eligible.append(m)

    return {
        "markets_seen": len(markets),
        "earliest_endDate": iso(min(dates)) if dates else None,
        "latest_endDate": iso(max(dates)) if dates else None,
        "volume_field_used": dict(vol_fields),
        "resolution_status_fields_present": dict(resolution_fields),
        "voided_count": voided,
        "by_year": {k: dict(v) for k, v in sorted(by_year.items())},
        "eligible_binary_above_floor": len(eligible),
    }, eligible


# ------------------------------------------------------------------ G3

def price_history(token_id, fidelity=None, interval=None, start=None, end=None):
    params = {"market": token_id}
    if interval:
        params["interval"] = interval
    if start and end:
        params["startTs"] = int(start / 1000)
        params["endTs"] = int(end / 1000)
    if fidelity:
        params["fidelity"] = fidelity
    try:
        data = get(CLOB + "/prices-history", params)
    except Exception as exc:
        return None, str(exc)
    return (data or {}).get("history", []), None


def probe_granularity(sample):
    """
    For each sampled market, walk fidelity from finest to coarsest and record the
    finest value that actually returns points. This is the charter 3 T=1 question.
    """
    finest_hits = Counter()
    per_fidelity = {f: {"ok": 0, "empty": 0, "error": 0, "points": []}
                    for f in FIDELITIES}
    errors = []

    for idx, m in enumerate(sample):
        tokens = parse_token_ids(m)
        if not tokens:
            continue
        token = tokens[0]  # YES leg
        end_ms = parse_dt(m.get("endDate"))
        if not end_ms:
            continue
        start_ms = end_ms - 35 * DAY_MS

        finest = None
        for fid in FIDELITIES:
            hist, err = price_history(token, fidelity=fid,
                                      start=start_ms, end=end_ms)
            if err:
                per_fidelity[fid]["error"] += 1
                if len(errors) < 20:
                    errors.append({"market": m.get("id"), "fidelity": fid,
                                   "error": err})
                continue
            if hist:
                per_fidelity[fid]["ok"] += 1
                per_fidelity[fid]["points"].append(len(hist))
                if finest is None:
                    finest = fid
            else:
                per_fidelity[fid]["empty"] += 1
        finest_hits[finest if finest is not None else "none"] += 1

        sys.stderr.write("\r  granularity probe %d/%d" % (idx + 1, len(sample)))
        sys.stderr.flush()
    sys.stderr.write("\n")

    for fid, rec in per_fidelity.items():
        pts = rec.pop("points")
        rec["median_points"] = sorted(pts)[len(pts) // 2] if pts else 0
    return {
        "finest_fidelity_that_returns_data": {str(k): v for k, v
                                              in finest_hits.items()},
        "per_fidelity": {str(k): v for k, v in per_fidelity.items()},
        "errors": errors,
    }


# ------------------------------------------------------------------ G4

def probe_checkpoints(sample, fidelity):
    """
    Charter 3: price at T days before resolution, T in {1,7,30}.
    A checkpoint is PRESENT if a price point exists inside the [T, T-1) day
    window before endDate. Absent is counted, never interpolated.
    Also records how far the nearest point actually sits from the checkpoint —
    that gap is what decides whether the definition is honest at this fidelity.
    """
    present = {t: 0 for t in CHECKPOINTS}
    absent = {t: 0 for t in CHECKPOINTS}
    gaps = {t: [] for t in CHECKPOINTS}
    usable = 0

    for idx, m in enumerate(sample):
        tokens = parse_token_ids(m)
        end_ms = parse_dt(m.get("endDate"))
        if not tokens or not end_ms:
            continue
        hist, err = price_history(tokens[0], fidelity=fidelity,
                                  start=end_ms - 35 * DAY_MS, end=end_ms)
        if err or not hist:
            for t in CHECKPOINTS:
                absent[t] += 1
            continue
        usable += 1
        stamps = []
        for point in hist:
            ts = point.get("t")
            if ts is None:
                continue
            stamps.append(ts * 1000 if ts < 1e11 else ts)  # sec or ms
        if not stamps:
            for t in CHECKPOINTS:
                absent[t] += 1
            continue

        for t in CHECKPOINTS:
            target = end_ms - t * DAY_MS
            window = [s for s in stamps if target <= s < target + DAY_MS]
            if window:
                present[t] += 1
            else:
                absent[t] += 1
            nearest = min(stamps, key=lambda s: abs(s - target))
            gaps[t].append(abs(nearest - target) / 3600000.0)  # hours

        sys.stderr.write("\r  checkpoint probe %d/%d" % (idx + 1, len(sample)))
        sys.stderr.flush()
    sys.stderr.write("\n")

    out = {"fidelity_used": fidelity, "markets_with_history": usable,
           "checkpoints": {}}
    for t in CHECKPOINTS:
        total = present[t] + absent[t]
        gap = sorted(gaps[t])
        out["checkpoints"]["T=%d" % t] = {
            "present": present[t],
            "absent": absent[t],
            "absence_rate": round(absent[t] / total, 4) if total else None,
            "median_gap_to_nearest_point_hours":
                round(gap[len(gap) // 2], 2) if gap else None,
            "p90_gap_hours":
                round(gap[int(len(gap) * 0.9)], 2) if gap else None,
            "EXCEEDS_30PCT_DOWNGRADE_THRESHOLD":
                bool(total and absent[t] / total > 0.30),
        }
    return out


# ------------------------------------------------------------------ main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=150,
                    help="markets to probe for granularity/checkpoints")
    ap.add_argument("--max-pages", type=int, default=12,
                    help="Gamma pages (500 markets each) to harvest")
    ap.add_argument("--out", default="probe_result.json")
    args = ap.parse_args()

    result = {"probed_at": datetime.now(timezone.utc).isoformat(),
              "charter": "PM2_charter_v1.md",
              "volume_floor": VOLUME_FLOOR}

    print("[G1/G2] harvesting resolved markets from Gamma ...")
    markets = harvest_markets(args.max_pages)

    # G0: measure the schema before trusting any assumed field name.
    census = field_census(markets)
    result["field_census"] = census
    print("\n[G0] Gamma schema census - %d distinct fields over %d markets"
          % (census["field_count"], census["total_markets_scanned"]))
    print("     status / resolution / volume candidates:")
    for key, info in census["status_and_resolution_fields"].items():
        print("       %-28s %5.1f%%  %-18s %s"
              % (key, info["present_pct"], list(info["types"]),
                 " ; ".join(info["samples"])[:95]))
    result["value_space"] = value_space(
        markets, list(census["status_and_resolution_fields"].keys()))
    print("\n     observed value distributions (charter must name ONE field):")
    print(json.dumps(result["value_space"], indent=2, ensure_ascii=False)[:2200])

    universe, eligible = summarise_universe(markets)
    result["universe"] = universe
    print("\n[G1/G2] universe summary")
    print(json.dumps(universe, indent=2)[:2500])

    if not eligible:
        print("\n!! No eligible binary markets above the volume floor. "
              "Charter 2 sampling rules cannot be executed as written.")
        result["verdict"] = "BLOCKED: empty eligible universe"
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        return 1

    # Even stride across the eligible list so the sample spans the whole period
    # rather than clustering in one year.
    stride = max(1, len(eligible) // args.sample)
    sample = eligible[::stride][:args.sample]
    result["sample_size"] = len(sample)
    print("\n[G3] probing /prices-history granularity on %d markets ..."
          % len(sample))
    gran = probe_granularity(sample)
    result["granularity"] = gran
    print(json.dumps(gran["finest_fidelity_that_returns_data"], indent=2))
    print(json.dumps(gran["per_fidelity"], indent=2))

    # Use the coarsest fidelity that actually works as the checkpoint basis.
    working = [int(f) for f, rec in gran["per_fidelity"].items()
               if rec["ok"] > rec["empty"]]
    chosen = min(working) if working else 1440
    print("\n[G4] checkpoint absence at fidelity=%d ..." % chosen)
    checks = probe_checkpoints(sample, chosen)
    result["checkpoints"] = checks
    print(json.dumps(checks, indent=2))

    flagged = [k for k, v in checks["checkpoints"].items()
               if v["EXCEEDS_30PCT_DOWNGRADE_THRESHOLD"]]
    result["windows_exceeding_30pct_absence"] = flagged
    result["verdict"] = ("DOWNGRADE REQUIRED for: " + ", ".join(flagged)
                         if flagged else "All three windows below 30% absence")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print("\n" + "=" * 60)
    print("VERDICT: %s" % result["verdict"])
    print("finest fidelity actually serving resolved markets: %d min" % chosen)
    print("wrote %s" % os.path.abspath(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
