# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Turn a simulation result into a trace.

This closes the loop: configuration -> engine -> trace -> viewer, with no
case-specific code anywhere in the path. `adapters/` exists only for wrapping
*foreign* simulators; anything running on this engine emits traces from here.

    cfg = load_experiment("configs/example.yaml")
    result = Simulator(cfg, congestion_minutes=40).run()
    emit(result).write("run.trace.json")
"""

from __future__ import annotations

from typing import Any

from .engine import (
    DUTY_LIMIT_REACHED, NO_CREW_ASSIGNED, RESOURCE_OUT_OF_POSITION, SeasonResult,
)
from .trace import Airport as TAirport, Leg as TLeg, TraceBuilder

REASON_TEXT = {
    DUTY_LIMIT_REACHED: "crew reached its legal duty limit",
    RESOURCE_OUT_OF_POSITION: "aircraft was not at the departure airport",
    NO_CREW_ASSIGNED: "no crew could legally be assigned to the duty",
}


def emit(result: SeasonResult, *, run_id: str | None = None, label: str | None = None,
         day_labels: list[str] | None = None, notes: str = "",
         extra_parameters: list[dict[str, Any]] | None = None) -> TraceBuilder:
    """Build a trace from a completed run.

    Parameters declared in the experiment configuration are carried through with
    their provenance intact, and the engine adds the ones it knows about itself,
    so the trace is self-describing without the caller having to restate anything.
    """
    cfg = result.config
    tb = TraceBuilder(
        run_id=run_id or cfg.name,
        label=label or cfg.description.strip().split("\n")[0] or cfg.name,
        engine="airresilience.engine",
        seed=cfg.seed,
        notes=notes or cfg.description,
    )

    tb.set_network(hub=cfg.network.hub, airports=[
        TAirport(code=a.code, name=a.name, lat=a.lat, lon=a.lon,
                 is_hub=(a.code == cfg.network.hub))
        for a in cfg.network.airports])

    ac_ids = sorted({l.aircraft for l in result.schedule})
    tb.set_aircraft(ac_ids)
    tb.set_crew_units(result.crew_units)

    tb.set_policy(standby_pct=cfg.policy.standby_pct,
                  roster_mode=cfg.policy.roster_mode,
                  repositioning_per_night=(cfg.policy.repositioning_per_night
                                           or cfg.fleet.repositioning_per_night),
                  regulation=cfg.regulation.name,
                  roster_basis=(cfg.baseline_regulation.name
                                if cfg.policy.roster_mode == "legacy" and cfg.baseline_regulation
                                else cfg.regulation.name),
                  notes=cfg.policy.notes)

    # Declared in the config, carried through unchanged.
    for p in list(cfg.parameters) + list(extra_parameters or []):
        tb.add_parameter(p["name"], p["value"], p.get("provenance", "user"),
                         p.get("unit", ""), p.get("note", ""))

    # Structural facts the engine knows without being told.
    tb.add_parameter("aircraft", cfg.fleet.count, "user", "count")
    tb.add_parameter("crew_units", result.crew_units, "derived", "units",
                     f"{cfg.crew.unit_size} licensed individuals per unit")
    tb.add_parameter("duty_lines_per_day", result.duties_per_day, "derived", "duties",
                     "Consequence of the roster cap applied to this timetable")
    tb.add_parameter("regulation", cfg.regulation.describe(),
                     "assumed" if cfg.regulation.is_reconstruction else "sourced", "",
                     cfg.regulation.authority)

    for d in result.days:
        tb.add_day(d.day,
                   (day_labels[d.day] if day_labels and d.day < len(day_labels)
                    else f"Day {d.day + 1}"),
                   conditions=d.conditions,
                   metrics={"cancelled": d.cancelled,
                            "cancel_pct": round(d.cancel_pct, 2),
                            "otp_pct": round(d.otp_pct, 2),
                            "delayed": d.delayed,
                            "stranded_overnight": d.stranded_overnight,
                            "standby_callouts": d.standby_callouts,
                            "unstaffed_duties": d.unstaffed_duties})

        for o in d.outcomes:
            l = o.leg
            tb.add_leg(TLeg(id=l.id + 1 + d.day * 100000, day=d.day, aircraft=l.aircraft,
                            frm=l.origin, to=l.destination,
                            sched_dep=l.scheduled_departure, block=l.block_minutes,
                            state=o.state, reason=o.reason,
                            dep=o.departure, arr=o.arrival, relieved=o.relieved))
            if o.state == "CNL":
                tb.add_event(d.day, o.departure if o.departure is not None
                             else l.scheduled_departure, "cancel", l.aircraft,
                             REASON_TEXT.get(o.reason or "", o.reason or ""))
        if d.stranded_overnight:
            tb.add_event(d.day, 1440, "strand", "",
                         f"{d.stranded_overnight} aircraft end the day away from the hub")
        if d.standby_callouts:
            tb.add_event(d.day, 0, "callout", "",
                         f"{d.standby_callouts} standby crews called out")

    tb.set_metrics(**result.summary())
    return tb
