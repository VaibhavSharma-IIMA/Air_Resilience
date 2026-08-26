# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Ingest US DOT BTS On-Time Performance data.

The Bureau of Transportation Statistics publishes every domestic US flight with
its tail number, scheduled and actual times, and a cancellation code. Tail
numbers are the reason this dataset is usable here: they let a real aircraft
rotation be reconstructed exactly, rather than assumed.

What this makes testable, and what it does not
----------------------------------------------
The model has two propagation mechanisms. Aircraft become stranded out of
position, and crews reach their legal duty limits. BTS supports the first
completely and the second not at all: **no public dataset anywhere records which
crew worked which flight**, so crew legality cannot be validated against
observation, only assumed.

The validation therefore runs with crew constraints disabled, using an
unconstrained rule set. Every duty is always staffable and always legal, so the
only cancellation the model can produce is `resource_out_of_position`. That is a
narrow claim, and it is a real one: it tests whether reconstructed rotations
under a delay model reproduce the propagation that actually occurred.

Re-enabling crew is a configuration change, not new code. Supply a real rule set
in place of `unconstrained_rules()` and the crew layer resumes; see
`docs/validation.md`.

Getting the data
----------------
BTS blocks scripted download, so fetch by hand:

    https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FGJ

Choose "Reporting Carrier On-Time Performance (1987-present)", pick a month, and
select at least these fields:

    FlightDate, Reporting_Airline, Tail_Number, Flight_Number_Reporting_Airline,
    Origin, Dest, CRSDepTime, DepTime, CRSArrTime, ArrTime, Cancelled,
    CancellationCode, DepDelay, ArrDelay, CRSElapsedTime

Unzip and point this module at the CSV.

Usage
-----
    python bts_ingest.py raw.csv --carrier WN --origin-hub MDW \\
        --start 2024-01-08 --end 2024-01-14 --out clean_week/

Two directories are typically produced, one calm week for calibration and one
disrupted week held out, and neither is fitted using the other.
"""

from __future__ import annotations

import argparse
import collections
import csv
import datetime as dt
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from airresilience.regulations import RuleSet          # noqa: E402

# BTS cancellation codes.
CANCEL_CODE = {"A": "carrier", "B": "weather", "C": "national air system", "D": "security"}

# BTS publishes the same table under two naming conventions depending on which
# download route is used: the field-selection tool emits CamelCase names, the
# prezipped and TranStats "T_ONTIME" exports emit SHOUTY_SNAKE. Aliasing both
# means a user does not have to care which link they clicked.
ALIASES = {
    "FlightDate":        ["FlightDate", "FL_DATE"],
    "Reporting_Airline": ["Reporting_Airline", "OP_UNIQUE_CARRIER", "OP_CARRIER",
                          "IATA_CODE_Reporting_Airline"],
    "Tail_Number":       ["Tail_Number", "TAIL_NUM"],
    "Origin":            ["Origin", "ORIGIN"],
    "Dest":              ["Dest", "DEST"],
    "CRSDepTime":        ["CRSDepTime", "CRS_DEP_TIME"],
    "DepTime":           ["DepTime", "DEP_TIME"],
    "CRSArrTime":        ["CRSArrTime", "CRS_ARR_TIME"],
    "CRSElapsedTime":    ["CRSElapsedTime", "CRS_ELAPSED_TIME"],
    "DepDelay":          ["DepDelay", "DEP_DELAY"],
    "Cancelled":         ["Cancelled", "CANCELLED"],
    "CancellationCode":  ["CancellationCode", "CANCELLATION_CODE"],
    "Diverted":          ["Diverted", "DIVERTED"],
    "LateAircraftDelay": ["LateAircraftDelay", "LATE_AIRCRAFT_DELAY"],
    "CarrierDelay":      ["CarrierDelay", "CARRIER_DELAY"],
    "WeatherDelay":      ["WeatherDelay", "WEATHER_DELAY"],
    "NASDelay":          ["NASDelay", "NAS_DELAY"],
}

# Without these the rotation cannot be reconstructed at all.
REQUIRED = ["FlightDate", "Tail_Number", "Origin", "Dest", "CRSDepTime"]


def unconstrained_rules() -> RuleSet:
    """A rule set that never binds.

    Every limit left unset is not enforced, so crews are always legal and always
    available. This isolates aircraft displacement as the sole cancellation
    mechanism, which is the only one BTS can adjudicate.
    """
    return RuleSet(
        name="unconstrained",
        description="Crew constraints disabled; aircraft rotations only",
        authority="not applicable",
        report_before_minutes=0,
        debrief_after_minutes=0,
        roster_headroom_minutes=0,
    )


def _hhmm(v: str) -> int | None:
    """BTS writes times as HHMM without a separator; 2400 means midnight."""
    v = (v or "").strip().strip('"')
    if not v or not v.isdigit():
        return None
    n = int(v)
    if n == 2400:
        return 0
    return (n // 100) * 60 + (n % 100)


def build_column_map(fieldnames: list[str]) -> dict[str, str]:
    """Map canonical names onto whatever this export happens to call them."""
    present = {c.strip().strip('"') for c in fieldnames if c}
    out: dict[str, str] = {}
    for canon, names in ALIASES.items():
        for n in names:
            if n in present:
                out[canon] = n
                break
    return out


def _parse_date(v: str) -> str:
    """Return YYYY-MM-DD from either ISO or the US format TranStats exports use."""
    v = (v or "").strip().strip('"').split()[0] if v else ""
    if not v:
        return ""
    if "-" in v:
        return v[:10]
    parts = v.split("/")
    if len(parts) == 3:
        m, d, y = parts
        return f"{y}-{int(m):02d}-{int(d):02d}"
    return v


def infer_timezone_offsets(rows: list[dict], reference: str) -> dict[str, int]:
    """Recover each airport's UTC offset, in minutes, from the schedule itself.

    BTS publishes times in the *local* time of each airport, but a simulation
    needs one clock: otherwise a westbound flight appears to land before it
    departed, and an aircraft looks double-booked. Rather than ship a timezone
    database, the offsets are solved from the data, which is self-consistent:

        arrival_local - departure_local = block + (offset_dest - offset_origin)

    so every flight gives the offset *difference* between its two airports.
    Propagating those differences outward from a reference airport by breadth
    first search fixes every airport the network reaches. Differences are taken
    as the median over all flights on a route, which absorbs the odd bad record.
    """
    import collections, statistics
    diffs: dict[tuple[str, str], list[int]] = collections.defaultdict(list)
    for r in rows:
        dep, arr = _hhmm(r.get("CRSDepTime", "")), _hhmm(r.get("CRSArrTime", ""))
        blk = r.get("CRSElapsedTime", "")
        if dep is None or arr is None or not blk:
            continue
        try:
            blk = int(float(blk))
        except ValueError:
            continue
        d = ((arr - dep) % 1440) - blk
        d = ((d + 720) % 1440) - 720          # wrap into +/- 12 h
        if abs(d) <= 720:
            diffs[(r["Origin"], r["Dest"])].append(d)

    edge = {k: int(round(statistics.median(v))) for k, v in diffs.items() if v}
    graph: dict[str, list[tuple[str, int]]] = collections.defaultdict(list)
    for (o, d), delta in edge.items():
        graph[o].append((d, delta))
        graph[d].append((o, -delta))

    offsets = {reference: 0}
    queue = collections.deque([reference])
    while queue:
        a = queue.popleft()
        for b, delta in graph[a]:
            if b not in offsets:
                offsets[b] = offsets[a] + delta
                queue.append(b)
    # Round to the quarter hour: real offsets are multiples of 15 minutes.
    return {k: int(round(v / 15.0)) * 15 for k, v in offsets.items()}


def _norm(row: dict, cmap: dict[str, str]) -> dict:
    """Rewrite a raw row into canonical field names."""
    clean = {k.strip().strip('"'): (v.strip().strip('"') if isinstance(v, str) else v)
             for k, v in row.items() if k}
    out = {canon: clean.get(actual, "") for canon, actual in cmap.items()}
    out["FlightDate"] = _parse_date(out.get("FlightDate", ""))
    return out


def read_bts(path: str, carrier: str | None = None,
             start: str | None = None, end: str | None = None) -> list[dict]:
    """Load and filter BTS rows, keeping only what the reconstruction needs."""
    out: list[dict] = []
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csv.DictReader(fh)
        cmap = build_column_map(reader.fieldnames or [])
        missing = [c for c in REQUIRED if c not in cmap]
        if missing:
            raise SystemExit(
                f"The export is missing required columns: {', '.join(missing)}\n"
                f"Found: {', '.join(sorted(c for c in (reader.fieldnames or [])))}\n\n"
                "Re-download from BTS with the fields listed in this module's docstring.")
        if "Cancelled" not in cmap and "DepTime" not in cmap:
            raise SystemExit(
                "The export has neither Cancelled nor DepTime, so cancellations "
                "cannot be identified. Re-download including at least one of them.")
        for raw in reader:
            r = _norm(raw, cmap)
            if carrier and r.get("Reporting_Airline", "") != carrier:
                continue
            d = r.get("FlightDate", "")
            if start and d < start:
                continue
            if end and d > end:
                continue
            tail = r.get("Tail_Number", "")
            if not tail:            # a flight with no tail cannot be placed in a rotation
                continue
            out.append(r)
    return out


def build_rotations(rows: list[dict], hub: str | None = None,
                    min_legs_per_tail: int = 2, offsets: dict[str, int] | None = None) -> dict:
    """Reconstruct each aircraft's real daily sequence of flights.

    Grouping by (tail, date) and sorting by scheduled departure recovers the
    rotation as operated. Cancelled flights stay in the schedule: they were
    published, and whether the model also loses them is precisely the question.
    """
    by_day: dict[str, list[dict]] = collections.defaultdict(list)
    for r in rows:
        by_day[r["FlightDate"]].append(r)

    dates = sorted(by_day)
    legs: list[dict] = []
    observed: dict[str, dict] = {}
    airports: set[str] = set()
    dropped_continuity = 0

    for day_index, date in enumerate(dates):
        per_tail: dict[str, list[dict]] = collections.defaultdict(list)
        for r in by_day[date]:
            per_tail[r["Tail_Number"]].append(r)

        for tail, flights in per_tail.items():
            flights = [f for f in flights if _hhmm(f["CRSDepTime"]) is not None]
            flights.sort(key=lambda f: _hhmm(f["CRSDepTime"])
                         - ((offsets or {}).get(f["Origin"], 0)))
            if len(flights) < min_legs_per_tail:
                continue

            # Within-day gaps are kept, not discarded. A gap means the operator
            # moved the aircraft by some means the passenger schedule does not
            # show, and the engine treats a gap the model did not cause as
            # planned repositioning rather than as a failure. Dropping these legs
            # would throw away a third of a disrupted week, which is exactly the
            # part worth studying.
            chain = flights
            dropped_continuity += sum(1 for a, b in zip(chain, chain[1:])
                                      if b["Origin"] != a["Dest"])

            for seq, f in enumerate(chain):
                dep = _hhmm(f["CRSDepTime"])
                if offsets is not None:
                    # Shift into the common frame so one clock governs the network.
                    dep = dep - offsets.get(f["Origin"], 0)
                block = f.get("CRSElapsedTime", "")
                block = int(float(block)) if block.replace(".", "").isdigit() else None
                if block is None:
                    arr = _hhmm(f.get("CRSArrTime", ""))
                    block = ((arr - dep) % 1440) if arr is not None else 60
                block = max(20, min(block, 900))
                lid = len(legs)
                legs.append({"id": lid, "aircraft": tail, "origin": f["Origin"],
                             "destination": f["Dest"], "scheduled_departure": dep,
                             "block_minutes": block, "day": day_index, "sequence": seq})
                airports.update((f["Origin"], f["Dest"]))
                # Prefer the explicit flag; fall back to "the aircraft never
                # departed", which is what a cancellation means operationally.
                if f.get("Cancelled", "") in ("1", "1.0", "1.00"):
                    cancelled = True
                elif f.get("Cancelled", "") in ("0", "0.0", "0.00"):
                    cancelled = False
                else:
                    cancelled = not (f.get("DepTime") or "").strip()
                def num(key):
                    v = (f.get(key) or "").strip()
                    try:
                        return float(v)
                    except ValueError:
                        return None
                observed[str(lid)] = {
                    "day": day_index,
                    "cancelled": cancelled,
                    "code": CANCEL_CODE.get(f.get("CancellationCode", ""), ""),
                    "diverted": f.get("Diverted", "") in ("1", "1.0", "1.00"),
                    "dep_delay": num("DepDelay"),
                    # BTS's own measure of knock-on delay from a late inbound
                    # aircraft. This is the same mechanism the model implements,
                    # so it is an independent target that does not involve
                    # cancellations at all.
                    "late_aircraft_delay": num("LateAircraftDelay"),
                    "carrier_delay": num("CarrierDelay"),
                    "weather_delay": num("WeatherDelay"),
                }

    return {"dates": dates, "legs": legs, "observed": observed,
            "airports": sorted(airports), "hub": hub,
            "dropped_continuity": dropped_continuity}


def write_schedule_csv(rot: dict, path: pathlib.Path) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["aircraft", "origin", "destination", "scheduled_departure",
                    "block_minutes", "day", "sequence", "id"])
        for l in rot["legs"]:
            w.writerow([l["aircraft"], l["origin"], l["destination"],
                        l["scheduled_departure"], l["block_minutes"],
                        l["day"], l["sequence"], l["id"]])


def write_config(rot: dict, out: pathlib.Path, name: str, hub: str) -> None:
    """Emit an experiment config with crew constraints switched off."""
    tails = sorted({l["aircraft"] for l in rot["legs"]})
    lines = [
        f"# {name}",
        "#",
        "# Generated from US DOT BTS On-Time Performance data by adapters/bts_ingest.py.",
        "# Aircraft rotations are reconstructed from real tail numbers; the schedule",
        "# is what the carrier published, including flights that were cancelled.",
        "#",
        "# Crew constraints are DISABLED. No public dataset records crew assignment,",
        "# so crew legality cannot be validated against observation. With an",
        "# unconstrained rule set every duty is always legal, and the only",
        "# cancellation the model can produce is resource_out_of_position.",
        "# To re-enable crew, replace the regulation block with a real rule set.",
        "",
        f"name: {name}",
        f"description: BTS-derived rotations, {rot['dates'][0]} to {rot['dates'][-1]}",
        f"days: {len(rot['dates'])}",
        "seed: 1",
        "",
        "network:",
        f"  hub: {hub}",
        "  airports:",
    ]
    for a in rot["airports"]:
        lines.append(f"    - {{code: {a}}}")
    lines += [
        "",
        "fleet:",
        f"  count: {len(tails)}",
        f"  base: {hub}",
        "  turn_minutes:  {hub: 40, outstation: 35}   # calibrated, see validate_bts.py",
        "  slack_minutes: {hub: 0, outstation: 0}",
        "  overnight_ground_minutes: 300",
        "  repositioning_per_night: 0                 # BTS shows no ferry positioning",
        "",
        "crew:",
        f"  units: {len(tails) * 4}                   # generous; crew must never bind",
        "  unit_size: 2",
        "  callout_minutes: 0",
        "",
        "schedule:",
        "  source: file",
        "  path: schedule.csv",
        "  records_actual: true          # historical data, so gaps are recovery not failure",
        "",
        "# Deliberately empty of limits: an unset rule is not enforced.",
        "regulation:",
        "  name: unconstrained",
        "  description: Crew constraints disabled; aircraft rotations only",
        "  authority: not applicable",
        "  report_before_minutes: 0",
        "  debrief_after_minutes: 0",
        "  roster_headroom_minutes: 0",
        "",
        "policy:",
        "  roster_mode: compliant",
        "  standby_pct: 0",
        "  repositioning_per_night: 0",
        "",
        "parameters:",
        "  - {name: rotations, value: BTS reconstruction, provenance: sourced,",
        "     note: US DOT Bureau of Transportation Statistics, On-Time Performance}",
        "  - {name: crew_constraints, value: disabled, provenance: assumed,",
        "     note: No public data records crew assignment; not validated}",
        "",
    ]
    (out / "config.yaml").write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("csv", help="BTS On-Time Performance export")
    ap.add_argument("--carrier", help="e.g. WN, AA, DL")
    ap.add_argument("--origin-hub", required=True, help="airport used as the reporting base")
    ap.add_argument("--start", help="YYYY-MM-DD inclusive")
    ap.add_argument("--end", help="YYYY-MM-DD inclusive")
    ap.add_argument("--out", required=True, help="directory to write into")
    ap.add_argument("--min-legs", type=int, default=2)
    ap.add_argument("--name", default=None)
    a = ap.parse_args()

    rows = read_bts(a.csv, a.carrier, a.start, a.end)
    if rows and not rows[0].get("Cancelled"):
        print("  note: no Cancelled column; inferring cancellation from a missing")
        print("        departure time. CancellationCode is also absent, so carrier")
        print("        and weather causes cannot be separated.\n")
    if not rows:
        raise SystemExit("No rows matched. Check the carrier code and the date range.")
    offsets = infer_timezone_offsets(rows, a.origin_hub)
    spread = (min(offsets.values()), max(offsets.values())) if offsets else (0, 0)
    print(f"  timezones        {len(offsets)} airports resolved, "
          f"offsets {spread[0]/60:+.0f}h to {spread[1]/60:+.0f}h relative to {a.origin_hub}")
    rot = build_rotations(rows, a.origin_hub, a.min_legs, offsets)
    if not rot["legs"]:
        raise SystemExit("No rotations could be reconstructed; try --min-legs 1.")

    out = pathlib.Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    write_schedule_csv(rot, out / "schedule.csv")
    json.dump(rot["observed"], (out / "observed.json").open("w", encoding="utf-8"))
    # Weather and airspace cancellations are decisions taken outside the
    # operator's control loop, largely in advance of the flight. They are the
    # trigger, not the cascade, so they are handed to the model as given.
    exo = sorted(int(k) for k, v in rot["observed"].items()
                 if v["cancelled"] and v["code"] in ("weather", "national air system"))
    json.dump(exo, (out / "exogenous.json").open("w", encoding="utf-8"))
    print(f"  exogenous        {len(exo):,} weather/airspace cancellations "
          f"(given to the model, not predicted by it)")
    name = a.name or f"bts-{a.carrier or 'all'}-{a.origin_hub}-{rot['dates'][0]}"
    write_config(rot, out, name, a.origin_hub)

    cancelled = sum(1 for o in rot["observed"].values() if o["cancelled"])
    codes = collections.Counter(o["code"] for o in rot["observed"].values()
                                if o["cancelled"] and o["code"])
    tails = len({l["aircraft"] for l in rot["legs"]})
    print(f"{name}")
    print(f"  dates            {rot['dates'][0]} to {rot['dates'][-1]}  "
          f"({len(rot['dates'])} days)")
    print(f"  aircraft         {tails}")
    print(f"  airports         {len(rot['airports'])}")
    print(f"  legs             {len(rot['legs']):,}")
    print(f"  cancelled        {cancelled:,} ({100*cancelled/len(rot['legs']):.2f}%)")
    if codes:
        print(f"  by cause         " + ", ".join(f"{k} {v}" for k, v in codes.most_common()))
    if rot["dropped_continuity"]:
        print(f"  planned gaps     {rot['dropped_continuity']} within-day repositionings "
              f"(kept, not treated as failures)")
    print(f"\n  wrote {out}/schedule.csv, config.yaml, observed.json")


if __name__ == "__main__":
    main()
