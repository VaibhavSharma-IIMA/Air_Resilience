# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
AirResilience trace format.

A *trace* is the standard record of one simulation run. It is deliberately
engine-agnostic: nothing in this module imports a simulator, and any model that
can describe its network, its legs and their outcomes can emit a valid trace.

That separation is the point. The simulator computes; the trace is the contract;
the viewer renders any trace without knowing which model produced it. It also
means the browser layer never re-implements the model, so there is only ever one
engine to maintain.

Format
------
    {
      "format": "airresilience.trace",
      "version": "1.0",
      "meta":       run identity and provenance
      "network":    hub, airports with coordinates
      "resources":  aircraft, crew unit count
      "policy":     the levers this run was configured with
      "parameters": every parameter with an explicit provenance label
      "days":       per-day labels, conditions and metrics
      "legs":       every flight leg with its planned and realised outcome
      "events":     notable state changes worth showing on a timeline
      "metrics":    run-level summary
    }

Times are integer minutes from local midnight of the day the leg belongs to, so
a leg is located by (day, sched_dep). A leg that lands after midnight simply has
arr > 1440; consumers should not assume arr < 1440.

Provenance
----------
Every parameter carries one of:

    sourced     taken from published external evidence
    user        supplied by whoever configured the experiment
    assumed     a modelling choice with no external evidence
    calibrated  fitted to reproduce an observed outcome
    derived     computed from other parameters

This is not decoration. A reader of a simulation result needs to know which
numbers are evidence and which are choices, and burying that in a paper appendix
is how simulation studies get over-read.
"""

from __future__ import annotations

import datetime as _dt
import json
import pathlib
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal

FORMAT = "airresilience.trace"
VERSION = "1.0"

Provenance = Literal["sourced", "user", "assumed", "calibrated", "derived"]
PROVENANCES = ("sourced", "user", "assumed", "calibrated", "derived")

LegState = Literal["OK", "DLY", "CNL"]
LEG_STATES = ("OK", "DLY", "CNL")


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

@dataclass
class Airport:
    """A node in the network. Coordinates are optional but strongly preferred.

    With lat/lon the viewer draws a real map. Without them it falls back to a
    radial layout around the hub, which is enough for a synthetic network.
    """
    code: str
    name: str = ""
    lat: float | None = None
    lon: float | None = None
    is_hub: bool = False

    def to_dict(self) -> dict:
        d = {"code": self.code, "name": self.name or self.code, "is_hub": self.is_hub}
        if self.lat is not None and self.lon is not None:
            d["lat"], d["lon"] = self.lat, self.lon
        return d


@dataclass
class Leg:
    """One scheduled flight on one day, together with what became of it."""
    id: int
    day: int
    aircraft: str
    frm: str
    to: str
    sched_dep: int              # minutes from local midnight
    block: int                  # scheduled block time, minutes
    state: LegState = "OK"
    reason: str | None = None   # why it was cancelled, if it was
    dep: int | None = None      # realised departure
    arr: int | None = None      # realised arrival
    duty: int | None = None     # duty line this leg belonged to
    relieved: bool = False      # a standby resource was called out for this leg

    def to_dict(self) -> dict:
        d = {"id": self.id, "day": self.day, "ac": self.aircraft,
             "from": self.frm, "to": self.to, "sd": self.sched_dep,
             "blk": self.block, "st": self.state}
        if self.reason:
            d["why"] = self.reason
        if self.dep is not None:
            d["dep"] = self.dep
        if self.arr is not None:
            d["arr"] = self.arr
        if self.duty is not None:
            d["duty"] = self.duty
        if self.relieved:
            d["relieved"] = True
        return d


@dataclass
class Parameter:
    name: str
    value: Any
    provenance: Provenance
    unit: str = ""
    note: str = ""

    def to_dict(self) -> dict:
        d = {"name": self.name, "value": self.value, "provenance": self.provenance}
        if self.unit:
            d["unit"] = self.unit
        if self.note:
            d["note"] = self.note
        return d


@dataclass
class Day:
    index: int
    label: str = ""
    conditions: list[str] = field(default_factory=list)   # e.g. ["fog"]
    metrics: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"index": self.index, "label": self.label or f"Day {self.index + 1}",
                "conditions": self.conditions, "metrics": self.metrics}


@dataclass
class Event:
    """Something worth marking on a timeline: a callout, a stranding, a closure."""
    day: int
    t: int
    type: str
    subject: str = ""
    detail: str = ""

    def to_dict(self) -> dict:
        return {"day": self.day, "t": self.t, "type": self.type,
                "subject": self.subject, "detail": self.detail}


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------

class TraceBuilder:
    """Accumulates a run and writes a valid trace.

    Usage is deliberately dumb, so that adapting an existing simulator to emit
    traces is a mechanical job rather than a redesign:

        tb = TraceBuilder(run_id="baseline", label="As flown")
        tb.set_network(hub="BOM", airports=[...])
        tb.add_parameter("standby_pct", 0, "user", "%")
        tb.add_day(0, "2 Dec", conditions=["fog"], metrics={...})
        tb.add_leg(Leg(...))
        tb.write("baseline.trace.json")
    """

    def __init__(self, run_id: str, label: str = "", engine: str = "",
                 seed: int | None = None, notes: str = ""):
        self.meta = {"run_id": run_id, "label": label or run_id, "engine": engine,
                     "seed": seed, "notes": notes,
                     "created": _dt.datetime.now(_dt.timezone.utc)
                                   .replace(microsecond=0).isoformat()}
        self.network: dict = {"hub": None, "airports": []}
        self.resources: dict = {"aircraft": [], "crew_units": None}
        self.policy: dict = {}
        self.parameters: list[Parameter] = []
        self.days: list[Day] = []
        self.legs: list[Leg] = []
        self.events: list[Event] = []
        self.metrics: dict = {}

    # -- configuration ------------------------------------------------------
    def set_network(self, hub: str, airports: Iterable[Airport]) -> "TraceBuilder":
        self.network["hub"] = hub
        self.network["airports"] = [a.to_dict() for a in airports]
        return self

    def set_aircraft(self, ids: Iterable[str], labels: dict[str, str] | None = None) -> "TraceBuilder":
        labels = labels or {}
        self.resources["aircraft"] = [{"id": i, "label": labels.get(i, i)} for i in ids]
        return self

    def set_crew_units(self, n: int) -> "TraceBuilder":
        self.resources["crew_units"] = n
        return self

    def set_policy(self, **kw) -> "TraceBuilder":
        self.policy.update(kw)
        return self

    def add_parameter(self, name: str, value: Any, provenance: Provenance,
                      unit: str = "", note: str = "") -> "TraceBuilder":
        if provenance not in PROVENANCES:
            raise ValueError(f"provenance must be one of {PROVENANCES}, got {provenance!r}")
        self.parameters.append(Parameter(name, value, provenance, unit, note))
        return self

    # -- run content --------------------------------------------------------
    def add_day(self, index: int, label: str = "", conditions: list[str] | None = None,
                metrics: dict | None = None) -> "TraceBuilder":
        self.days.append(Day(index, label, list(conditions or []), dict(metrics or {})))
        return self

    def add_leg(self, leg: Leg) -> "TraceBuilder":
        self.legs.append(leg)
        return self

    def add_event(self, day: int, t: int, type: str, subject: str = "", detail: str = "") -> "TraceBuilder":
        self.events.append(Event(day, t, type, subject, detail))
        return self

    def set_metrics(self, **kw) -> "TraceBuilder":
        self.metrics.update(kw)
        return self

    # -- output -------------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "format": FORMAT, "version": VERSION,
            "meta": self.meta,
            "network": self.network,
            "resources": self.resources,
            "policy": self.policy,
            "parameters": [p.to_dict() for p in self.parameters],
            "days": [d.to_dict() for d in self.days],
            "legs": [l.to_dict() for l in self.legs],
            "events": [e.to_dict() for e in self.events],
            "metrics": self.metrics,
        }

    def write(self, path: str | pathlib.Path, indent: int | None = None) -> pathlib.Path:
        p = pathlib.Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        validate(self.to_dict())
        p.write_text(json.dumps(self.to_dict(), separators=(",", ":"), indent=indent),
                 encoding="utf-8")
        return p


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

class TraceError(ValueError):
    """Raised when a trace does not satisfy the format contract."""


def validate(trace: dict) -> dict:
    """Check a trace against the format. Returns it unchanged, or raises.

    Kept deliberately strict on the things a viewer depends on and permissive on
    everything else, so that engines can attach their own extra fields without
    the format having to know about them.
    """
    def need(cond, msg):
        if not cond:
            raise TraceError(msg)

    need(trace.get("format") == FORMAT, f"format must be {FORMAT!r}")
    need("version" in trace, "missing version")

    net = trace.get("network") or {}
    codes = {a["code"] for a in net.get("airports", [])}
    need(net.get("hub"), "network.hub is required")
    need(net["hub"] in codes, f"hub {net.get('hub')!r} is not among the airports")

    ac_ids = {a["id"] for a in (trace.get("resources") or {}).get("aircraft", [])}
    day_ix = {d["index"] for d in trace.get("days", [])}
    need(day_ix, "at least one day is required")

    for p in trace.get("parameters", []):
        need(p.get("provenance") in PROVENANCES,
             f"parameter {p.get('name')!r} has invalid provenance {p.get('provenance')!r}")

    seen = set()
    for l in trace.get("legs", []):
        key = (l["day"], l["id"])
        need(key not in seen, f"duplicate leg id {l['id']} on day {l['day']}")
        seen.add(key)
        need(l["st"] in LEG_STATES, f"leg {l['id']} has invalid state {l['st']!r}")
        need(l["day"] in day_ix, f"leg {l['id']} refers to unknown day {l['day']}")
        need(l["from"] in codes and l["to"] in codes,
             f"leg {l['id']} refers to an airport outside the network")
        if ac_ids:
            need(l["ac"] in ac_ids, f"leg {l['id']} refers to unknown aircraft {l['ac']!r}")
        if l["st"] == "CNL":
            need("dep" not in l or l.get("dep") is None or True, "")
        else:
            need(l.get("dep") is not None and l.get("arr") is not None,
                 f"leg {l['id']} is {l['st']} but has no realised times")
    return trace


def read(path: str | pathlib.Path) -> dict:
    """Load and validate a trace from disk."""
    return validate(json.loads(pathlib.Path(path).read_text(encoding="utf-8")))


def summarise(trace: dict) -> dict:
    """Derive the standard headline metrics from a trace.

    Computed from the legs rather than trusted from `metrics`, so that two traces
    from different engines are always compared on the same definitions.
    """
    legs = trace["legs"]
    total = len(legs)
    cnl = [l for l in legs if l["st"] == "CNL"]
    dly = [l for l in legs if l["st"] == "DLY"]
    flown = total - len(cnl)
    by_reason: dict[str, int] = {}
    for l in cnl:
        by_reason[l.get("why", "unspecified")] = by_reason.get(l.get("why", "unspecified"), 0) + 1
    delays = [l["dep"] - l["sd"] for l in legs if l["st"] != "CNL" and l.get("dep") is not None]
    return {
        "legs": total,
        "cancelled": len(cnl),
        "cancel_pct": round(100 * len(cnl) / total, 2) if total else 0.0,
        "delayed": len(dly),
        "flown": flown,
        "otp_pct": round(100 * (flown - len(dly)) / flown, 2) if flown else 100.0,
        "mean_delay_min": round(sum(delays) / len(delays), 1) if delays else 0.0,
        "cancellations_by_reason": by_reason,
    }
