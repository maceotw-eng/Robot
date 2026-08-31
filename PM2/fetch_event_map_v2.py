#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_event_map_v2.py - market -> event mapping via /events, WALL-AWARE.

The v1 by-event route paged /events with a bare offset and died at the
deep-offset wall (offset 2000 -> mapped 1 / 100,000). Same Gamma, same wall
as /markets (measured 2026-08-25: offset<=2000 ok, >=2100 -> 422).

This port reuses the harvest design that finished the 314,169-market universe:
  - timestamp-bisected end_date windows, id-sorted paging under the ceiling
  - a 1-page PROBE at offset=ceiling decides overflow before any full read
  - lo < hi invariant (zero-length ranges are refused as "invalid time range")
  - filter honoured? VERIFIED FIRST: Gamma silently ignores unknown params
    (id_min lesson), so the date filter on /events is checked against the
    returned endDates before anything is trusted
  - resume journal + streamed pairs, so a killed run continues
  - API error bodies are carried in every failure message

Output format matches fetch_event_map.py so pm2_pipeline.py --event-map
consumes it unchanged.

    python3 fetch_event_map_v2.py sample_100k.ndjson --out event_map.json
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

GAMMA = "https://gamma-api.polymarket.com"
UA = {"User-Agent": "pm2-evmap/2.0", "Accept": "application/json"}
OFFSET_CEILING = 1900
PAGE = 100


class ApiError(Exception):
    def __init__(self, code, body, url=""):
        self.code, self.body, self.url = code, body, url
        super().__init__("HTTP %s: %s" % (code, body))


class DepthWall(Exception):
    pass


def get(url, params, retries=4, timeout=40):
    full = url + "?" + urllib.parse.urlencode(params)
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(full, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(min(60, 2 ** attempt))
                last = exc
                continue
            body = ""
            try:
                body = exc.read().decode("utf-8", "replace")[:300]
            except Exception:
                pass
            raise ApiError(exc.code, body or str(exc.reason), full)
        except (urllib.error.URLError, OSError) as exc:
            time.sleep(min(30, 1.5 * (attempt + 1)))
            last = exc
    raise RuntimeError("exhausted retries: %s (%s)" % (full, last))


def _ts_epoch(ts):
    return int(datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp())


def _ts_iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _end_of(ev):
    return str(ev.get("endDate") or "")


def verify_date_filter(lo, hi):
    """Gamma ignores unknown params silently. Prove end_date_min/max bite on /events."""
    rows = get(GAMMA + "/events", {"closed": "true", "limit": 50, "order": "id",
                                   "ascending": "true",
                                   "end_date_min": lo, "end_date_max": hi})
    if not isinstance(rows, list) or not rows:
        return False, "filtered page empty (window %s..%s)" % (lo, hi)
    outside = [_end_of(e) for e in rows
               if _end_of(e) and not (lo <= _end_of(e)[:19] + "Z" <= hi)]
    if outside:
        return False, "filter NOT honoured on /events: %d/%d rows outside window, e.g. %s" \
            % (len(outside), len(rows), outside[:2])
    return True, "verified: %d/%d rows inside window" % (len(rows), len(rows))


class Harvest:
    def __init__(self, sink, journal_path, wanted):
        self.sink = sink            # callable(event) -> None
        self.journal_path = journal_path
        self.pages = 0
        self.seen_events = set()
        self.done = []              # completed [lo_e, hi_e] intervals
        self.wanted = wanted
        if os.path.exists(journal_path):
            for line in open(journal_path):
                p = line.strip().split("|")
                if len(p) >= 2:
                    try:
                        self.done.append([int(p[0]), int(p[1])])
                    except ValueError:
                        pass
        self.jfh = open(journal_path, "a")

    def covered(self, lo_e, hi_e):
        return any(a <= lo_e and hi_e <= b for a, b in self.done)

    def page(self, params):
        try:
            rows = get(GAMMA + "/events", params)
        except ApiError as exc:
            if exc.code == 422 and int(params.get("offset", 0)) > 0:
                raise DepthWall("422 at offset=%s | api: %s | params: %s"
                                % (params.get("offset"), exc.body,
                                   {k: v for k, v in params.items() if k != "limit"}))
            raise
        self.pages += 1
        if not isinstance(rows, list):
            return []
        for ev in rows:
            eid = ev.get("id")
            if eid is not None and eid not in self.seen_events:
                self.seen_events.add(eid)
                self.sink(ev)
        return rows

    def scan(self, lo, hi, asc=True):
        """Page one window up to the ceiling. Returns (hit_ceiling, min_id, max_id)."""
        base = {"closed": "true", "limit": PAGE, "order": "id",
                "ascending": "true" if asc else "false",
                "end_date_min": lo, "end_date_max": hi}
        offset, min_id, max_id = 0, None, None
        while offset <= OFFSET_CEILING:
            rows = self.page(dict(base, offset=offset))
            if not rows:
                return False, min_id, max_id
            for ev in rows:
                try:
                    i = int(ev.get("id"))
                except (TypeError, ValueError):
                    continue
                min_id = i if min_id is None else min(min_id, i)
                max_id = i if max_id is None else max(max_id, i)
            offset += len(rows)
        return True, min_id, max_id

    def run(self, lo, hi, max_pages):
        stack = [(lo, hi)]
        bisections = dual = 0
        while stack:
            if self.pages >= max_pages:
                sys.stderr.write("  NOTE: page budget exhausted - PARTIAL\n")
                return {"truncated": True, "bisections": bisections, "dual": dual}
            lo_s, hi_s = stack.pop()
            lo_e, hi_e = _ts_epoch(lo_s), _ts_epoch(hi_s)
            if self.covered(lo_e, hi_e):
                continue
            # probe page at the ceiling
            probe = self.page({"closed": "true", "limit": PAGE, "order": "id",
                               "ascending": "true", "end_date_min": lo_s,
                               "end_date_max": hi_s, "offset": OFFSET_CEILING})
            overflow = bool(probe)
            if overflow and hi_e - lo_e >= 3:
                mid_e = lo_e + (hi_e - lo_e) // 2
                stack.append((_ts_iso(mid_e + 1), hi_s))
                stack.append((lo_s, _ts_iso(mid_e)))
                bisections += 1
                continue
            hit, min_a, max_a = self.scan(lo_s, hi_s, asc=True)
            if not hit:
                self.jfh.write("%d|%d\n" % (lo_e, hi_e)); self.jfh.flush()
            else:
                dual += 1
                _, min_d, _ = self.scan(lo_s, hi_s, asc=False)
                if min_d is not None and max_a is not None and min_d <= max_a + 1:
                    self.jfh.write("%d|%d\n" % (lo_e, hi_e)); self.jfh.flush()
                else:
                    raise DepthWall("span %s..%s exceeds 2x ceiling (asc top id %s, "
                                    "desc bottom id %s)" % (lo_s, hi_s, max_a, min_d))
            sys.stderr.write("\r  pages=%d events=%d mapped_wanted=%d   "
                             % (self.pages, len(self.seen_events), self.wanted_hits()))
        sys.stderr.write("\n")
        return {"truncated": False, "bisections": bisections, "dual": dual}

    def wanted_hits(self):
        return len(self.wanted & self._mapped_keys) if hasattr(self, "_mapped_keys") else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("sample")
    ap.add_argument("--out", default="event_map.json")
    ap.add_argument("--state-dir", default="evmap_state")
    ap.add_argument("--date-min", default="2023-01-01T00:00:00Z")
    ap.add_argument("--date-max", default="2026-12-31T23:59:59Z")
    ap.add_argument("--max-pages", type=int, default=20000)
    args = ap.parse_args()

    wanted = set()
    with open(args.sample) as fh:
        for line in fh:
            line = line.strip()
            if line:
                try:
                    wanted.add(str(json.loads(line).get("id")))
                except Exception:
                    pass
    print("sample: %d markets" % len(wanted))

    ok, note = verify_date_filter("2025-03-01T00:00:00Z", "2025-03-02T23:59:59Z")
    print("date filter on /events: %s (%s)" % ("OK" if ok else "FAIL", note))
    if not ok:
        print("!! Cannot bisect without a working date filter. Stopping.")
        return 2

    os.makedirs(args.state_dir, exist_ok=True)
    pairs_path = os.path.join(args.state_dir, "pairs.ndjson")
    mapping = {}
    if os.path.exists(pairs_path):
        for line in open(pairs_path):
            try:
                m, e = line.rstrip("\n").split("\t")
                mapping[m] = e
            except ValueError:
                pass
        print("resume: %d pairs on disk" % len(mapping))
    pfh = open(pairs_path, "a")

    def sink(ev):
        eid = str(ev.get("id"))
        for mk in (ev.get("markets") or []):
            mid = str(mk.get("id"))
            if mid not in mapping:
                mapping[mid] = eid
                pfh.write("%s\t%s\n" % (mid, eid))
    h = Harvest(sink, os.path.join(args.state_dir, "journal.txt"), wanted)
    h._mapped_keys = mapping.keys()

    t0 = time.time()
    try:
        meta = h.run(args.date_min, args.date_max, args.max_pages)
    except DepthWall as exc:
        print("\nHARVEST BLOCKED: %s" % exc)
        meta = {"truncated": True, "blocked": str(exc)}
    finally:
        pfh.flush(); pfh.close()

    sub = {m: mapping[m] for m in wanted if m in mapping}
    sizes = Counter(sub.values())
    multi = sum(1 for v in sizes.values() if v > 1)
    out = {"route": "by-event-bisect", "markets_in_sample": len(wanted),
           "mapped": len(sub),
           "coverage": round(len(sub) / len(wanted), 4) if wanted else 0,
           "distinct_events": len(sizes), "multi_leg_events": multi,
           "largest_event_legs": max(sizes.values()) if sizes else 0,
           "all_pairs_harvested": len(mapping), "events_seen": len(h.seen_events),
           "pages": h.pages, "elapsed_min": round((time.time() - t0) / 60, 1),
           "harvest_meta": meta, "map": sub}
    with open(args.out, "w") as fh:
        json.dump(out, fh, ensure_ascii=False)
    print("\nmapped %d / %d (%.2f%%) | events %d | multi-leg %d | largest %d legs | "
          "pages %d | %.1f min"
          % (len(sub), len(wanted), 100.0 * len(sub) / max(1, len(wanted)),
             len(sizes), multi, out["largest_event_legs"], h.pages, out["elapsed_min"]))
    print("wrote %s" % args.out)
    if meta.get("truncated"):
        print("!! PARTIAL - coverage figure above is the truth; do not treat as complete.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
