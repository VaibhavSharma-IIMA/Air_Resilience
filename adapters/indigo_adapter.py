# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Worked example: adapting an outside simulator to the AirResilience trace format.

The simulator adapted here is `reference/indigo_model.py`, a self-contained
implementation of the hub case that knows nothing about this framework. That is
the point: it stands in for any simulator someone else has already written.

This module is the only place that knows both that simulator and the trace
format. Neither imports the other. Adapting a different simulator to the viewer
and the analysis tools means writing one file like this and nothing else.

Run:
    python indigo_adapter.py            # writes the standard example traces
    python indigo_adapter.py --help
"""

from __future__ import annotations

import argparse
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "reference"))

from airresilience.trace import Airport, Leg, TraceBuilder, read, summarise  # noqa: E402

try:
    import indigo_model as M  # noqa: E402
    from indigo_model import (  # noqa: E402
        CALIBRATED_CREWS_PER_AC, DEFAULT_FOG_DAYS, FITTED_CONGESTION, FITTED_FDTL_HOURS,
        StructuralConfig, base_profile, run_season,
    )
except ModuleNotFoundError:
    sys.exit(
        "This adapter wraps the standalone simulator in\n"
        f"{ROOT / 'reference'}. It is only an example of adapting a foreign\n"
        "simulator. To emit traces from the framework itself, use:\n\n"
        "    python run.py configs/indigo_bom.yaml --trace out.trace.json")

# Real coordinates, so the viewer draws an actual network rather than a diagram.
# A synthetic study can omit these; the viewer falls back to a radial layout.
AIRPORTS = {
    "BOM": ("Mumbai",     19.0896, 72.8656),
    "AMD": ("Ahmedabad",  23.0772, 72.6347),
    "JAI": ("Jaipur",     26.8242, 75.8122),
    "DEL": ("Delhi",      28.5562, 77.1000),
    "CCU": ("Kolkata",    22.6547, 88.4467),
    "HYD": ("Hyderabad",  17.2403, 78.4294),
    "MAA": ("Chennai",    12.9941, 80.1709),
    "BLR": ("Bengaluru",  13.1979, 77.7063),
    "GOI": ("Goa",        15.3808, 73.8314),
}

DAY_LABELS = ["2 Dec", "3 Dec", "4 Dec", "5 Dec", "6 Dec", "7 Dec", "8 Dec"]

REASON_TEXT = {
    "fdtl": "crew reached its legal duty limit",
    "oop":  "aircraft was not at the departure airport",
    "unstaffed": "no crew could legally be assigned to the duty",
}


def build_trace(run_id: str, label: str, *, seed: int = 104, standby_pct: float = 0.0,
                roster_mode: str = "legacy", fdtl_hours: float = FITTED_FDTL_HOURS,
                crews_per_ac: float = CALIBRATED_CREWS_PER_AC, notes: str = "") -> TraceBuilder:
    prof = base_profile(crews_per_ac)
    cfg = StructuralConfig(congestion=FITTED_CONGESTION)
    r = run_season(seed, prof, fdtl_hours=fdtl_hours, fog_days=DEFAULT_FOG_DAYS,
                   roster_mode=roster_mode, standby_pct=standby_pct, cfg=cfg,
                   record_all=True)

    tb = TraceBuilder(run_id=run_id, label=label,
                      engine=f"indigo_model (case study) / airresilience-adapter",
                      seed=seed, notes=notes)

    tb.set_network(hub=M.HUB, airports=[
        Airport(code=c, name=n, lat=la, lon=lo, is_hub=(c == M.HUB))
        for c, (n, la, lo) in AIRPORTS.items()])

    tails = sorted({l.tail for l in r["all_days"][0]["legs"]})
    tb.set_aircraft([f"AC{t:02d}" for t in tails],
                    {f"AC{t:02d}": f"6E{1100 + t * 7}" for t in tails})
    tb.set_crew_units(r["crews"])

    tb.set_policy(standby_pct=standby_pct, roster_mode=roster_mode,
                  repositioning_per_night=6,
                  roster_basis="published under the pre-change duty cap"
                               if roster_mode == "legacy" else
                               "rebuilt to comply with the revised cap")

    # Provenance is the point of this block. A reader should be able to see at a
    # glance which of these numbers are evidence and which are our choices.
    tb.add_parameter("fdtl_cut", fdtl_hours, "calibrated", "hours",
                     "Fitted jointly with congestion_feedback to four published outcomes")
    tb.add_parameter("congestion_feedback", cfg.congestion, "calibrated", "minutes",
                     "Added delay at full disruption; functional form is assumed, magnitude fitted")
    tb.add_parameter("crews_per_aircraft", crews_per_ac, "derived", "crews/aircraft",
                     "Set so the pre-change week runs clean; only the 7.6:9.1 ratio is sourced")
    tb.add_parameter("standby_pct", standby_pct, "user", "%")
    tb.add_parameter("aircraft", len(tails), "assumed", "count", "Stylised sub-fleet")
    tb.add_parameter("base_duty_cap", M.BASE_CAP, "assumed", "minutes",
                     "13 h; our reconstruction of the pre-change limit, not read from the circular")
    tb.add_parameter("min_rest", cfg.min_rest, "assumed", "minutes")
    tb.add_parameter("weekly_duty_cap", M.WEEKLY_DUTY_CAP, "assumed", "minutes", "60 h per rolling 7 days")
    tb.add_parameter("turn_spoke", prof.turn_spoke, "assumed", "minutes")
    tb.add_parameter("turn_hub", prof.turn_hub, "assumed", "minutes")
    tb.add_parameter("overnight_ground", cfg.overnight_min, "assumed", "minutes")
    tb.add_parameter("weekly_departures_network", M.WEEKLY_DEPARTURES, "sourced", "flights",
                     "DGCA notice")
    tb.add_parameter("pilot_establishment_network", M.PILOTS, "sourced", "pilots", "Lok Sabha")

    leg_uid = 0
    for d, rec in enumerate(r["all_days"]):
        day = r["days"][d]
        tb.add_day(d, DAY_LABELS[d] if d < len(DAY_LABELS) else f"Day {d+1}",
                   conditions=["fog"] if d in DEFAULT_FOG_DAYS else [],
                   metrics={"cancelled": day["cnl"], "cancel_pct": round(day["cnl_pct"], 2),
                            "otp_pct": round(day["otp"], 2), "delayed": day["dly"],
                            "stranded_overnight": day["stranded"],
                            "standby_callouts": day["calls"],
                            "unstaffed_duties": day["unstaffed"]})

        for l in rec["legs"]:
            leg_uid += 1
            tb.add_leg(Leg(id=leg_uid, day=d, aircraft=f"AC{l.tail:02d}",
                           frm=l.frm, to=l.to, sched_dep=l.sched_dep, block=l.block,
                           state=l.state or "CNL", reason=l.reason,
                           dep=l.dep, arr=l.arr, duty=None))
            if l.state == "CNL":
                tb.add_event(d, l.dep if l.dep is not None else l.sched_dep, "cancel",
                             f"AC{l.tail:02d}", REASON_TEXT.get(l.reason or "", l.reason or ""))
        if day["stranded"]:
            tb.add_event(d, 1440, "strand", "",
                         f"{day['stranded']} aircraft end the day away from the hub")
        if day["calls"]:
            tb.add_event(d, 0, "callout", "", f"{day['calls']} standby crews called out")

    s = r["summary"]
    tb.set_metrics(legs=s["total"], cancelled=s["cnl"], cancel_pct=round(s["cnl_pct"], 2),
                   duty_lines_per_day=r["duties"], crew_units=r["crews"],
                   peak_day=s["peak_day"], peak_cancel_pct=round(s["peak_pct"], 2))
    return tb


SCENARIOS = {
    "asflown":    dict(label="As flown, no standby",
                       standby_pct=0, roster_mode="legacy",
                       notes="The roster published under the old duty cap, flown into the new one."),
    "standby12":  dict(label="12% standby",
                       standby_pct=12, roster_mode="legacy",
                       notes="Identical schedule, shock and weather; standby cover is the only change."),
    "replanned":  dict(label="Roster replanned to comply",
                       standby_pct=0, roster_mode="replanned",
                       notes="Duties rebuilt under the revised cap before the week starts."),
    "nochange":   dict(label="No rule change (counterfactual)",
                       standby_pct=0, roster_mode="legacy", fdtl_hours=0.0,
                       notes="The same week without the FDTL revision."),
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(HERE.parent / "examples"),
                    help="directory to write traces into")
    ap.add_argument("--seed", type=int, default=104)
    ap.add_argument("--only", nargs="*", choices=sorted(SCENARIOS),
                    help="build only these scenarios")
    a = ap.parse_args()

    out = pathlib.Path(a.out)
    names = a.only or list(SCENARIOS)
    print(f"{'scenario':<14}{'legs':>7}{'cancelled':>11}{'rate':>9}{'file':>34}")
    print("-" * 76)
    for name in names:
        tb = build_trace(name, seed=a.seed, **SCENARIOS[name])
        p = tb.write(out / f"indigo_{name}.trace.json")
        m = summarise(read(p))
        print(f"{name:<14}{m['legs']:>7}{m['cancelled']:>11}{m['cancel_pct']:>8.1f}%"
              f"{p.name:>34}")


if __name__ == "__main__":
    main()
