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


class ApiError(Exception):
    """
    HTTP error that CARRIES THE RESPONSE BODY, so the API's own explanation is
    never discarded. Lesson (2026-08-25): the prices-history 400s were
    mis-diagnosed twice in a row by reasoning from the outside (wrong id ->
    legacy stratum), while the response body had named the true cause all
    along: "invalid filters: 'startTs' and 'endTs' interval is too long".
    Let the error message speak before constructing an explanation.
    """
    def __init__(self, code, body, url=""):
        self.code = code
        self.body = body
        self.url = url
        super().__init__("HTTP %s: %s" % (code, body))


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
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise ApiError(exc.code, body or str(exc.reason), url)
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


def to_num(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def token_status(market):
    """
    A-1 mapping census. v1 silently skipped markets with unusable clobTokenIds,
    which is exactly the count the charter asks to be reported, not dropped.
    """
    raw = market.get("clobTokenIds")
    if raw in (None, "", [], {}):
        return None, "missing"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, "unparseable_string"
    if not isinstance(raw, list):
        return None, "not_a_list"
    if not raw:
        return None, "empty_list"
    tokens = [str(t) for t in raw if t not in (None, "")]
    if not tokens:
        return None, "all_blank"
    # Post-2023 CLOB tokens are ~77-digit uint256 strings. Short integer ids are
    # the pre-CLOB stratum and will not resolve against /prices-history.
    if not all(t.isdigit() for t in tokens):
        return tokens, "non_numeric"
    if len(tokens[0]) < 30:
        return tokens, "legacy_short_id"
    return tokens, "ok"


def settlement_payout(market):
    """
    Read the AUTHORITATIVE resolution off Gamma.

    Once a market resolves, Gamma sets outcomePrices to the PAYOUT vector:
    exactly one "1" and the rest "0" (e.g. Trump ["1","0"], DeSantis ["0","1"]).
    That is a settlement payout, not a traded price - which is why reading it is
    NOT the circularity trap that R-02 rules out on the Binance side. The trap is
    inferring an outcome from where the market was TRADING; this is the payout
    the venue actually pays.

    The test is therefore EXACT equality to 1 and 0. A closed market still
    showing ["0.99","0.01"] has not settled - it is merely near-certain, and
    counting it would reintroduce exactly the bias R-02 exists to prevent.
    """
    raw = market.get("outcomePrices")
    if raw in (None, "", [], {}):
        return None, "missing"
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return None, "unparseable_string"
    if not isinstance(raw, list) or not raw:
        return None, "not_a_list"

    vals = [to_num(v) for v in raw]
    if any(v is None for v in vals):
        return None, "non_numeric"
    if all(v == 0.0 for v in vals):
        return None, "all_zero"
    winners = [i for i, v in enumerate(vals) if v == 1.0]
    zeros = [v for v in vals if v == 0.0]
    if len(winners) == 1 and len(zeros) == len(vals) - 1:
        return winners[0], "settled_payout"
    return None, "unsettled_prices"


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

# Gamma refuses deep offsets. Measured 2026-08-25 by controlled experiment:
#   offset=2000 -> HTTP 200 (returns rows)
#   offset=2100 / 3000 / 5000 -> HTTP 422
# So the usable offset range is [0, 2000]. Keep a page of margin under it.
OFFSET_CEILING = 1900


class DepthWall(Exception):
    """A single endDate day holds more rows than the offset ceiling allows."""


def _batch(params):
    try:
        return get(GAMMA + "/markets", params)
    except (urllib.error.HTTPError, ApiError) as exc:
        if getattr(exc, "code", None) == 422:
            raise DepthWall("HTTP 422 at offset=%s - deep-offset wall"
                            % params.get("offset"))
        raise


def harvest_markets(max_pages, page_size=500, date_min=None, date_max=None,
                    order="endDate", ascending=True):
    """
    Harvest closed markets across the charter window using a DATE CURSOR.

    Three instrument faults have been found here, each only visible at a
    different scale, and each of which produced plausible-looking output:

    1. v1 - SHORT-PAGE STOP. It broke out as soon as a page came back shorter
       than the requested limit. Gamma caps this query around 100 rows however
       large a limit you ask for, so that fired on page one and the harvest
       truncated at 100 markets. Combined with an unbounded ascending endDate
       sort, the whole sample landed in a pre-2023 legacy stratum (earliest
       endDate 2011-07-05 - before the venue existed).

    2. v2 - DEEP-OFFSET WALL. Bounding the date window and paging until an
       empty page fixed (1), and then died with HTTP 422 on page 22. A
       controlled experiment pinned it: offset=2000 succeeds, offset>=2100
       returns 422, while date and sort parameters behave fine at shallow
       offsets. Same wall as (1), hit from the other side.

    3. v3 - this. Never let offset grow without bound. Page within a window up
       to OFFSET_CEILING, then advance end_date_min to the last endDate seen
       and restart at offset 0.

    end_date_min is inclusive and day-granular, so each cursor advance re-reads
    the boundary day; ids are deduped, which is also what makes a market whose
    day straddles a batch boundary come through exactly once.

    If one single day holds more rows than OFFSET_CEILING the cursor cannot
    advance. That is reported as DepthWall rather than looping forever or,
    worse, silently returning a partial harvest that still looks complete.
    """
    markets, seen_ids = [], set()
    cursor = date_min
    pages_used = 0
    windows = 0
    truncated = False

    while pages_used < max_pages:
        windows += 1
        offset = 0
        window_new = 0
        window_rows = 0
        last_day = None

        while offset <= OFFSET_CEILING and pages_used < max_pages:
            params = {"closed": "true", "limit": page_size, "offset": offset,
                      "order": order,
                      "ascending": "true" if ascending else "false"}
            if cursor:
                params["end_date_min"] = cursor
            if date_max:
                params["end_date_max"] = date_max

            batch = _batch(params)
            pages_used += 1
            if not isinstance(batch, list) or not batch:
                break

            window_rows += len(batch)
            for market in batch:
                day = iso(parse_dt(market.get("endDate")))
                if day and (last_day is None or day > last_day):
                    last_day = day
                mid = market.get("id")
                if mid is not None and mid in seen_ids:
                    continue
                if mid is not None:
                    seen_ids.add(mid)
                markets.append(market)
                window_new += 1

            offset += len(batch)
            sys.stderr.write("\r  window %d (from %s) offset %-5d | +%d new | %d total"
                             % (windows, cursor, offset, window_new, len(markets)))
            sys.stderr.flush()

        # ---- advance the cursor -----------------------------------------
        if last_day is None or window_rows == 0:
            break                                  # window empty: done
        if cursor is not None and last_day <= cursor and offset > OFFSET_CEILING:
            # The window filled the entire offset budget without the date
            # moving on: this single day is deeper than the wall.
            raise DepthWall(
                "endDate %s holds more than %d rows; the date cursor cannot "
                "advance past it. Narrow the query (e.g. by category) for that "
                "day, or page it by a secondary key." % (cursor, OFFSET_CEILING))
        if last_day == cursor and window_new == 0:
            break                                  # nothing new left to read
        if date_max and last_day > date_max:
            break
        cursor = last_day
    else:
        truncated = True

    sys.stderr.write("\n")
    if truncated:
        sys.stderr.write("  NOTE: stopped at max_pages=%d - coverage is PARTIAL, "
                         "raise --max-pages\n" % max_pages)
    sys.stderr.write("  harvested %d unique markets over %d date windows\n"
                     % (len(markets), windows))
    HARVEST_META.clear()
    HARVEST_META.update({"pages_used": pages_used, "max_pages": max_pages,
                         "date_windows": windows, "truncated": truncated,
                         "markets_harvested": len(markets)})
    return markets


# Filled by harvest_markets on every run; main() copies it into the report so
# a truncated harvest can never masquerade as a complete one (v3 hit its page
# budget at latest_endDate 2025-05-12 vs a requested window end of 2026-06-30,
# and the report carried no trace of it).
HARVEST_META = {}


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
    by_year = defaultdict(lambda: {"all": 0, "above_floor": 0, "binary": 0,
                                   "settled_payout": 0, "eligible": 0})
    dates, eligible = [], []
    resolution_fields = Counter()
    token_reasons = Counter()
    payout_reasons = Counter()
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

        for key in ("umaResolutionStatus", "umaResolutionStatuses",
                    "resolutionSource", "resolvedBy"):
            if m.get(key):
                resolution_fields[key] += 1
        status = str(m.get("umaResolutionStatus") or "").lower()
        if "void" in status or "invalid" in status:
            voided += 1

        # A-1 mapping census: count failures instead of silently skipping them.
        tokens, treason = token_status(m)
        token_reasons[treason] += 1

        # R-02 Polymarket side: is the settlement readable straight off Gamma?
        winner, preason = settlement_payout(m)
        payout_reasons[preason] += 1

        by_year[year]["all"] += 1
        if is_binary:
            by_year[year]["binary"] += 1
        if preason == "settled_payout":
            by_year[year]["settled_payout"] += 1
        if vol >= VOLUME_FLOOR:
            by_year[year]["above_floor"] += 1
            if is_binary and treason == "ok":
                m["_pm2_winner_index"] = winner
                m["_pm2_payout_reason"] = preason
                eligible.append(m)
                by_year[year]["eligible"] += 1

    total = max(1, len(markets))
    settled = payout_reasons.get("settled_payout", 0)
    return {
        "markets_seen": len(markets),
        "earliest_endDate": iso(min(dates)) if dates else None,
        "latest_endDate": iso(max(dates)) if dates else None,
        "volume_field_used": dict(vol_fields),
        "resolution_status_fields_present": dict(resolution_fields),
        "voided_count": voided,
        "token_mapping_census": dict(token_reasons),
        "token_mapping_failure_rate": round(
            1.0 - token_reasons.get("ok", 0) / total, 4),
        "settlement_payout_census": dict(payout_reasons),
        "settlement_readable_rate": round(settled / total, 4),
        "R02_polymarket_path": (
            "MAIN: outcomePrices carries an exact 1/0 payout for %.1f%% of markets "
            "- read actual frequency straight off Gamma" % (100.0 * settled / total)
            if settled / total >= 0.90 else
            "FALLBACK NEEDED: only %.1f%% of markets expose an exact 1/0 payout; "
            "use umaResolutionStatus or on-chain payoutNumerators for the rest"
            % (100.0 * settled / total)),
        "by_year": {k: dict(v) for k, v in sorted(by_year.items())},
        "eligible_binary_above_floor": len(eligible),
    }, eligible


# ------------------------------------------------------------------ G3

# CLOB rejects startTs/endTs spans it considers too long for the requested
# fidelity ("invalid filters: 'startTs' and 'endTs' interval is too long",
# measured 2026-08-25: a 1-year span at fidelity=1440 is refused, while
# interval=max at the same fidelity returns the full series). Window sizes
# below are scaled to fidelity to stay under that limit on the first attempt.
FID_WINDOW_DAYS = {1: 2, 10: 7, 60: 14, 360: 35, 720: 35, 1440: 35}


def _norm_ts_ms(ts):
    return ts * 1000 if ts < 1e11 else ts


def price_history(token_id, fidelity, end_ms, window_days=None):
    """
    Fetch price history near a market's resolution.

    Attempt order (each proven necessary by a distinct measured failure):
      1. windowed startTs/endTs sized to the fidelity  -> mode "window"
      2. on span rejection OR an empty window, interval=max fetched whole and
         sliced client-side to the same window          -> mode "interval_max"
    Returns (history, err, mode). history=[] with err=None is a MEASUREMENT
    (this market has no points in the window at this fidelity), not an error.
    """
    days = window_days or FID_WINDOW_DAYS.get(fidelity, 35)
    start_ms = end_ms - days * DAY_MS
    first_err = None
    try:
        data = get(CLOB + "/prices-history",
                   {"market": token_id, "fidelity": fidelity,
                    "startTs": int(start_ms / 1000), "endTs": int(end_ms / 1000)})
        hist = (data or {}).get("history", [])
        if hist:
            return hist, None, "window"
    except Exception as exc:
        first_err = str(exc)

    # Fallback: whole series at this fidelity, sliced to the window locally.
    try:
        data = get(CLOB + "/prices-history",
                   {"market": token_id, "interval": "max", "fidelity": fidelity})
        full = (data or {}).get("history", [])
        lo, hi = start_ms, end_ms + DAY_MS
        hist = [p for p in full if p.get("t") is not None
                and lo <= _norm_ts_ms(p["t"]) <= hi]
        return hist, None, "interval_max"
    except Exception as exc2:
        err = ("window: %s | interval=max: %s" % (first_err, exc2)
               if first_err else "interval=max: %s" % exc2)
        return None, err, "failed"


def probe_granularity(sample):
    """
    For each sampled market, walk fidelity from finest to coarsest and record the
    finest value that actually returns points. This is the charter 3 T=1 question.
    """
    finest_hits = Counter()
    per_fidelity = {f: {"ok": 0, "empty": 0, "error": 0,
                        "via_window": 0, "via_interval_max": 0, "points": []}
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

        finest = None
        for fid in FIDELITIES:
            hist, err, mode = price_history(token, fid, end_ms)
            if mode == "window":
                per_fidelity[fid]["via_window"] += 1
            elif mode == "interval_max":
                per_fidelity[fid]["via_interval_max"] += 1
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
    absent_no_history = 0    # fetch succeeded, series empty  -> measurement
    absent_fetch_error = 0   # fetch failed                   -> instrument/API

    for idx, m in enumerate(sample):
        tokens = parse_token_ids(m)
        end_ms = parse_dt(m.get("endDate"))
        if not tokens or not end_ms:
            continue
        hist, err, _mode = price_history(tokens[0], fidelity, end_ms,
                                         window_days=35)
        if err or not hist:
            if err:
                absent_fetch_error += 1
            else:
                absent_no_history += 1
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
           "absent_breakdown": {"no_history": absent_no_history,
                                "fetch_error": absent_fetch_error},
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
    ap.add_argument("--max-pages", type=int, default=600,
                    help="total page budget across all date windows")
    ap.add_argument("--date-min", default="2023-01-01",
                    help="charter 2 period start (end_date_min)")
    ap.add_argument("--date-max", default="2026-06-30",
                    help="charter 2 period end (end_date_max)")
    ap.add_argument("--out", default="probe_result.json")
    args = ap.parse_args()

    result = {"probed_at": datetime.now(timezone.utc).isoformat(),
              "charter": "PM2_charter_v1.1_DRAFT.md",
              "probe_version": "4.0",
              "volume_floor": VOLUME_FLOOR,
              "window": {"end_date_min": args.date_min,
                         "end_date_max": args.date_max}}

    print("[G1/G2] harvesting resolved markets from Gamma, window %s .. %s"
          % (args.date_min, args.date_max))
    try:
        markets = harvest_markets(args.max_pages, date_min=args.date_min,
                                  date_max=args.date_max)
    except DepthWall as exc:
        print("\nHARVEST BLOCKED: %s" % exc)
        result["verdict"] = "BLOCKED: %s" % exc
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        return 1

    # Completeness declaration: a truncated harvest must say so, loudly.
    latest_harvested = max((iso(parse_dt(m.get("endDate"))) or ""
                            for m in markets), default=None)
    result["completeness"] = dict(
        HARVEST_META,
        latest_endDate_harvested=latest_harvested,
        window_max_requested=args.date_max,
        full_window_covered=not HARVEST_META.get("truncated", False))
    print("\n[G1/G2] completeness: %s" % json.dumps(result["completeness"]))

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

    # Stratify by resolution year so every year-slice the charter needs
    # (2023 / 2024 / 2025-26, per 3.7 and 5 criterion 4) is represented,
    # rather than letting whichever year is most numerous dominate the sample.
    buckets = defaultdict(list)
    for market in eligible:
        end_ms = parse_dt(market.get("endDate"))
        buckets[iso(end_ms)[:4] if end_ms else "unknown"].append(market)
    years = sorted(buckets)
    per_year = max(1, args.sample // max(1, len(years)))
    sample = []
    for year in years:
        rows = buckets[year]
        stride = max(1, len(rows) // per_year)
        sample.extend(rows[::stride][:per_year])
    sample = sample[:args.sample]
    result["sample_size"] = len(sample)
    result["sample_by_year"] = {
        y: sum(1 for m in sample
               if (iso(parse_dt(m.get("endDate"))) or "----")[:4] == y)
        for y in years}
    print("\n  sample stratified by year: %s" % result["sample_by_year"])
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
    verdict = ("DOWNGRADE REQUIRED for: " + ", ".join(flagged)
               if flagged else "All three windows below 30% absence")
    if HARVEST_META.get("truncated"):
        verdict = ("INCOMPLETE HARVEST (page budget exhausted at %s, window "
                   "runs to %s) - raise --max-pages and rerun; "
                   % (latest_harvested, args.date_max)) + verdict
    result["verdict"] = verdict

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=2)
    print("\n" + "=" * 60)
    print("VERDICT: %s" % result["verdict"])
    print("finest fidelity actually serving resolved markets: %d min" % chosen)
    print("wrote %s" % os.path.abspath(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
