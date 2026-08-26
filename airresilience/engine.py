# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
The simulation engine.

Reads an `ExperimentConfig` and runs a season. Nothing here is specific to any
airline, network or regulation: the rules come from a `RuleSet`, the network and
fleet from configuration, and the schedule either from a file or from the
synthetic generator.

The mechanism
-------------
Each day, every leg is considered in scheduled-departure order and asked four
questions:

    1. is the aircraft at the departure airport?      no  -> cancel (out of position)
    2. when can it actually push back?                    -> delay accumulates
    3. is the crew still legal on arrival?            no  -> try standby, else cancel
    4. does the duty have a crew at all?              no  -> cancel (unstaffed)

Then position and readiness carry into the next morning, subject to overnight
repositioning capacity. That carry-over is what makes a week different from
seven independent days, and it is where propagation comes from.

Determinism
-----------
Two independent streams are drawn: one seeded on `seed` for schedule generation,
one on `seed + STREAM_OFFSET` for operational noise. Consumption order is fixed,
so a given configuration and seed always produce the same run. This matters more
than it sounds: it is what makes replication, calibration and regression testing
possible at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .model import (
    Aircraft, CrewUnit, Duty, ExperimentConfig, FlightLeg, Route,
)
from .regulations import RuleSet

STREAM_OFFSET = 4242
DAY_MINUTES = 1440

# Cancellation reasons.
#
# Deliberately named for the *mechanism* rather than for any regulation: a crew
# running out of legal hours is the same event whether the limit comes from the
# DGCA, the FAA or a collective agreement. Analysis code should compare against
# these constants rather than string literals.
RESOURCE_OUT_OF_POSITION = "resource_out_of_position"
DUTY_LIMIT_REACHED = "duty_limit_reached"
NO_CREW_ASSIGNED = "no_crew_assigned"

#: Cancellations caused directly by the shock, as opposed to propagated ones.
DIRECT_REASONS = (DUTY_LIMIT_REACHED,)
#: Cancellations that follow from earlier losses rather than from the shock.
PROPAGATED_REASONS = (RESOURCE_OUT_OF_POSITION, NO_CREW_ASSIGNED)
#: Imposed from outside the model rather than produced by it.
EXOGENOUS = "exogenous"


# ---------------------------------------------------------------------------
# Random stream
# ---------------------------------------------------------------------------

_U32 = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    r = (a * b) & _U32
    return r - 0x100000000 if r >= 0x80000000 else r


def rng_stream(seed: int) -> Callable[[], float]:
    """Deterministic 32-bit PRNG (mulberry32).

    Chosen over `random.Random` because the identical algorithm is trivially
    reimplementable in any language, which keeps cross-implementation
    verification possible.
    """
    state = seed & _U32

    def rand() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & _U32
        a = state
        t = _imul(a ^ (a >> 15), 1 | a) & _U32
        t = (((t + _imul(t ^ (t >> 7), 61 | t)) & _U32) ^ t) & _U32
        return ((t ^ (t >> 14)) & _U32) / 4294967296.0

    return rand


def _round_half_up(x: float) -> int:
    """Round halves away from zero, so behaviour matches across languages."""
    import math
    return math.floor(x + 0.5)


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------

@dataclass
class LegOutcome:
    leg: FlightLeg
    state: str = "OK"                   # OK | DLY | CNL
    reason: str | None = None
    departure: int | None = None
    arrival: int | None = None
    relieved: bool = False              # standby crew was called out
    noise: int = 0

    @property
    def delay(self) -> int:
        return 0 if self.departure is None else self.departure - self.leg.scheduled_departure


@dataclass
class DayResult:
    day: int
    outcomes: list[LegOutcome]
    unstaffed_duties: int = 0
    stranded_overnight: int = 0
    standby_available: int = 0
    standby_callouts: int = 0
    conditions: list[str] = field(default_factory=list)

    @property
    def legs(self) -> int:
        return len(self.outcomes)

    @property
    def cancelled(self) -> int:
        return sum(1 for o in self.outcomes if o.state == "CNL")

    @property
    def delayed(self) -> int:
        return sum(1 for o in self.outcomes if o.state == "DLY")

    @property
    def on_time(self) -> int:
        return sum(1 for o in self.outcomes if o.state == "OK")

    @property
    def cancel_pct(self) -> float:
        return 100 * self.cancelled / self.legs if self.legs else 0.0

    @property
    def otp_pct(self) -> float:
        flown = self.legs - self.cancelled
        return 100 * self.on_time / flown if flown else 100.0

    def by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            if o.state == "CNL":
                out[o.reason or "unspecified"] = out.get(o.reason or "unspecified", 0) + 1
        return out


@dataclass
class SeasonResult:
    config: ExperimentConfig
    days: list[DayResult]
    duties_per_day: int
    crew_units: int
    schedule: list[FlightLeg]

    @property
    def legs(self) -> int:
        return sum(d.legs for d in self.days)

    @property
    def cancelled(self) -> int:
        return sum(d.cancelled for d in self.days)

    @property
    def cancel_pct(self) -> float:
        return 100 * self.cancelled / self.legs if self.legs else 0.0

    @property
    def delayed(self) -> int:
        return sum(d.delayed for d in self.days)

    @property
    def on_time(self) -> int:
        return sum(d.on_time for d in self.days)

    @property
    def otp_pct(self) -> float:
        """On-time departures as a share of everything that actually flew.

        The season-level counterpart of `DayResult.otp_pct`, so punctuality can
        be replicated, attributed and swept in the same way as cancellations
        rather than only inspected one day at a time.
        """
        flown = self.legs - self.cancelled
        return 100 * self.on_time / flown if flown else 100.0

    def by_reason(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for d in self.days:
            for k, v in d.by_reason().items():
                out[k] = out.get(k, 0) + v
        return out

    def direct(self) -> int:
        """Cancellations caused directly by the shock."""
        r = self.by_reason()
        return sum(r.get(k, 0) for k in DIRECT_REASONS)

    def exogenous(self) -> int:
        """Cancellations imposed on the model rather than produced by it."""
        return self.by_reason().get(EXOGENOUS, 0)

    def propagated(self) -> int:
        """Cancellations that followed from earlier losses."""
        r = self.by_reason()
        return sum(r.get(k, 0) for k in PROPAGATED_REASONS)

    def cascade_multiplier(self) -> float:
        """Total cancellations per cancellation caused directly by the shock.

        A value near 1 means losses stay where they start. Larger values mean the
        network is propagating them, and the lever that matters is recovery
        capacity rather than the size of the initiating event.
        """
        d = self.direct()
        return ((d + self.propagated()) / d) if d else float("nan")

    def summary(self) -> dict[str, Any]:
        peak = max(self.days, key=lambda d: d.cancel_pct) if self.days else None
        return {
            "legs": self.legs, "cancelled": self.cancelled,
            "cancel_pct": round(self.cancel_pct, 3),
            "by_reason": self.by_reason(),
            "direct": self.direct(), "propagated": self.propagated(),
            "cascade_multiplier": round(self.cascade_multiplier(), 3) if self.direct() else None,
            "duties_per_day": self.duties_per_day,
            "crew_units": self.crew_units,
            "peak_day": peak.day if peak else None,
            "peak_cancel_pct": round(peak.cancel_pct, 3) if peak else None,
        }


# ---------------------------------------------------------------------------
# Schedule generation
# ---------------------------------------------------------------------------

def generate_schedule(cfg: ExperimentConfig) -> list[FlightLeg]:
    """Build one day's timetable of out-and-back rotations from the hub.

    A deliberately simple generator: it exists so a researcher without a real
    timetable can still produce a plausible hub operation. Anyone with real data
    should use `schedule.source: file` instead.
    """
    rand = rng_stream(cfg.seed)
    sc, fl, net = cfg.schedule, cfg.fleet, cfg.network
    hub = net.hub

    outbound = [r for r in net.routes if r.origin == hub] or [
        Route(hub, a.code, 60) for a in net.airports if a.code != hub]
    total_weight = sum(r.weight for r in outbound)

    def pick() -> Route:
        x = rand() * total_weight
        for r in outbound:
            x -= r.weight
            if x <= 0:
                return r
        return outbound[0]

    legs: list[FlightLeg] = []
    for i in range(fl.count):
        ac = f"AC{i:02d}"
        start = (sc.day_start
                 + _round_half_up(i * (sc.stagger_minutes / fl.count))
                 + _round_half_up(rand() * sc.stagger_jitter))
        pairs = sc.rotations_early if start < sc.early_cutoff else sc.rotations_late
        clock, seq = start, 0
        for _ in range(pairs):
            r = pick()
            legs.append(FlightLeg(len(legs), ac, hub, r.destination, clock, r.block_minutes, 0, seq))
            seq += 1
            clock += r.block_minutes + fl.turn(False) + fl.slack(False)
            legs.append(FlightLeg(len(legs), ac, r.destination, hub, clock, r.block_minutes, 0, seq))
            seq += 1
            clock += r.block_minutes + fl.turn(True) + fl.slack(True)
    return legs


def load_schedule_csv(path: str) -> list[FlightLeg]:
    """Read a timetable from CSV.

    Required columns: aircraft, origin, destination, scheduled_departure, block_minutes
    Optional:         id, day, sequence

    `scheduled_departure` may be minutes from midnight or HH:MM.
    """
    import csv
    legs: list[FlightLeg] = []
    with open(path, newline="", encoding="utf-8") as fh:
        for i, row in enumerate(csv.DictReader(fh)):
            sd = row["scheduled_departure"].strip()
            minutes = (int(sd.split(":")[0]) * 60 + int(sd.split(":")[1])) if ":" in sd else int(sd)
            legs.append(FlightLeg(
                id=int(row.get("id", i)), aircraft=row["aircraft"].strip(),
                origin=row["origin"].strip(), destination=row["destination"].strip(),
                scheduled_departure=minutes, block_minutes=int(row["block_minutes"]),
                day=int(row.get("day", 0) or 0), sequence=int(row.get("sequence", 0) or 0)))
    legs.sort(key=lambda l: (l.aircraft, l.scheduled_departure))
    for l in legs:
        pass
    return legs


def build_schedule(cfg: ExperimentConfig) -> list[FlightLeg]:
    """Return the legs for an experiment, read from file or generated.

    Which one is decided by `cfg.schedule.source`, so a caller does not need to
    know whether a study uses a real timetable or a synthetic one.
    """
    if cfg.schedule.source == "file":
        return load_schedule_csv(cfg.schedule.path)
    return generate_schedule(cfg)


def schedule_by_day(schedule: Sequence[FlightLeg], days: int) -> list[list[FlightLeg]]:
    """Split a schedule into the legs flown on each day.

    Two shapes are supported, and which one applies is inferred rather than
    configured:

    *Repeating* - every leg carries day 0, meaning one day's pattern flown each
    day. This is what the synthetic generator produces and what a stylised study
    normally wants.

    *Dated* - legs carry distinct day values, meaning a real timetable in which
    each day differs. Any schedule read from a file with more than one day value
    is treated this way, because repeating it would fly a week's flights every
    day.
    """
    dated = {l.day for l in schedule}
    if len(dated) <= 1:
        return [[FlightLeg(l.id, l.aircraft, l.origin, l.destination,
                           l.scheduled_departure, l.block_minutes, d, l.sequence)
                 for l in schedule] for d in range(days)]
    out: list[list[FlightLeg]] = []
    for d in range(days):
        out.append(sorted((l for l in schedule if l.day == d),
                          key=lambda x: x.scheduled_departure))
    return out


# ---------------------------------------------------------------------------
# Rostering
# ---------------------------------------------------------------------------

def build_duties(schedule: Sequence[FlightLeg], rules: RuleSet) -> list[Duty]:
    """Group each aircraft's legs into duties that respect the planning cap.

    A tighter cap produces shorter duties and therefore more of them for the same
    flying. That inflation is a structural consequence of a rule change and is
    knowable before any disruption occurs.
    """
    by_ac: dict[str, list[FlightLeg]] = {}
    for l in schedule:
        by_ac.setdefault(l.aircraft, []).append(l)

    duties: list[Duty] = []
    for ac in sorted(by_ac):
        current: Duty | None = None
        for l in sorted(by_ac[ac], key=lambda x: x.scheduled_departure):
            would_end = l.scheduled_arrival + rules.debrief_after_minutes
            cap = rules.roster_cap(False)
            legs_ok = (rules.max_legs_per_duty is None
                       or (current is not None and len(current.leg_ids) < rules.max_legs_per_duty))
            fits = (current is not None and legs_ok
                    and (cap is None or (would_end - current.start) <= cap))
            if fits:
                current.leg_ids.append(l.id)            # type: ignore[union-attr]
            else:
                current = Duty(len(duties), ac, [l.id],
                               l.scheduled_departure - rules.report_before_minutes)
                duties.append(current)
    return duties


def finalise_duties(duties: list[Duty], legs_by_id: dict[int, FlightLeg], rules: RuleSet) -> None:
    for d in duties:
        last = legs_by_id[d.leg_ids[-1]]
        d.end = last.scheduled_arrival + rules.debrief_after_minutes
        d.touches_night = rules.duty_touches_night(d.start, d.end)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class Simulator:
    """Runs a configured experiment.

        sim = Simulator(load_experiment("configs/example.yaml"))
        result = sim.run()
        print(result.summary())
    """

    def __init__(self, cfg: ExperimentConfig,
                 congestion_minutes: float = 0.0,
                 condition_effects: dict[str, dict[str, Any]] | None = None,
                 exogenous_cancellations: set[int] | None = None):
        self.cfg = cfg.validate()
        self.congestion = congestion_minutes
        # Legs removed by something outside the model: a storm, a closure, a
        # decision taken in advance. The simulation does not predict these; it
        # takes them as given and propagates their consequences. That separation
        # is what makes the propagation mechanism testable on its own, because
        # the trigger comes from the record and only the cascade is modelled.
        self.exogenous = exogenous_cancellations or set()
        # Weather and similar conditions are described here rather than hard-coded,
        # so a study can define its own without touching the engine.
        self.condition_effects = condition_effects or {
            "fog": {"before": 500, "base": 10, "spread": 18},
        }

    # -- helpers ------------------------------------------------------------
    def _roster_rules(self) -> RuleSet:
        """Which rules the roster was *planned* under.

        'legacy' means the airline kept flying a roster built under the previous
        regulation. That distinction is the difference between a schedule that is
        merely tight and one that is not staffable at all.
        """
        if self.cfg.policy.roster_mode == "legacy" and self.cfg.baseline_regulation:
            return self.cfg.baseline_regulation
        return self.cfg.regulation

    # -- main ---------------------------------------------------------------
    def run(self) -> SeasonResult:
        cfg = self.cfg
        rules = cfg.regulation
        noise = rng_stream(cfg.seed + STREAM_OFFSET)

        schedule = build_schedule(cfg)
        per_day = schedule_by_day(schedule, cfg.days)
        legs_by_id = {l.id: l for l in schedule}
        # A repeating schedule is rostered once; a dated one is rostered per day,
        # because each day's flying is different.
        repeating = len({l.day for l in schedule}) <= 1
        roster_rules = self._roster_rules()
        if repeating:
            duties = build_duties(per_day[0], roster_rules)
            finalise_duties(duties, {l.id: l for l in per_day[0]}, roster_rules)
            duties_per_day = [duties] * cfg.days
        else:
            duties_per_day = []
            for legs_d in per_day:
                du = build_duties(legs_d, roster_rules)
                finalise_duties(du, {l.id: l for l in legs_d}, roster_rules)
                duties_per_day.append(du)
            duties = duties_per_day[0]

        n_units = cfg.crew.resolve_units(cfg.fleet.count)
        crews = [CrewUnit(id=f"CU{i:03d}", base=cfg.crew.base or cfg.network.hub,
                          size=cfg.crew.unit_size) for i in range(n_units)]

        # Start each aircraft where its own first leg departs from, not at the
        # hub. A generated hub schedule makes these the same; a real timetable
        # does not, and a point-to-point network has no hub to start at.
        first_origin: dict[str, str] = {}
        for l in sorted(per_day[0] or schedule, key=lambda x: x.scheduled_departure):
            first_origin.setdefault(l.aircraft, l.origin)
        for l in sorted(schedule, key=lambda x: (x.day, x.scheduled_departure)):
            first_origin.setdefault(l.aircraft, l.origin)
        ids = sorted(first_origin) or [f"AC{i:02d}" for i in range(cfg.fleet.count)]
        aircraft = {i: Aircraft(id=i, base=first_origin.get(i, cfg.network.hub),
                                location=first_origin.get(i, cfg.network.hub),
                                ready_at=cfg.schedule.day_start - 60)
                    for i in ids}
        # Where the plan expects each aircraft to begin each day. Only meaningful
        # for dated schedules; a repeating one starts every day the same way.
        planned_start: list[dict[str, str]] = []
        if not repeating:
            for legs_d in per_day:
                first: dict[str, str] = {}
                for l in sorted(legs_d, key=lambda x: x.scheduled_departure):
                    first.setdefault(l.aircraft, l.origin)
                planned_start.append(first)

        # Aircraft the *model* has knocked out of position, as distinct from
        # aircraft the operator planned to move. A real timetable contains
        # positioning the passenger schedule does not show: ferry flights,
        # maintenance moves, overnight repositioning. Those appear as gaps in the
        # plan and are not failures. Only displacement the simulation itself
        # caused should propagate, or every planned gap becomes a phantom cascade.
        displaced: set[str] = set()
        trust_plan = cfg.schedule.is_actual

        trust_plan = bool(getattr(cfg.schedule, "records_actual", False))
        rolling_windows = [r.days for r in rules.rolling]
        max_consec = rules.max_consecutive_days()
        repositioning = cfg.policy.repositioning_per_night or cfg.fleet.repositioning_per_night

        results: list[DayResult] = []
        for day in range(cfg.days):
            conditions = cfg.conditions.get(day, [])
            t0 = day * DAY_MINUTES
            day_legs = per_day[day]
            duties = duties_per_day[day]
            duty_of = {lid: d for d in duties for lid in d.leg_ids}

            # --- who may work today ---
            pool = [c for c in crews if c.consecutive_days < max_consec]
            for c in pool:
                c._roll = {w: c.rolling_minutes(day, w) for w in rolling_windows}  # type: ignore[attr-defined]

            # --- assign crew units to duties, least-loaded first ---
            assigned: dict[int, CrewUnit] = {}
            busy: set[str] = set()
            unstaffed = 0
            for duty in sorted(duties, key=lambda d: d.start):
                candidates = [
                    c for c in pool
                    if c.id not in busy
                    and rules.rest_satisfied((t0 + duty.start) - c.last_duty_end)
                    and rules.rolling_ok(c._roll, duty.minutes)      # type: ignore[attr-defined]
                ]
                if not candidates:
                    unstaffed += 1
                    continue
                candidates.sort(key=lambda c: sum(c._roll.values()))  # type: ignore[attr-defined]
                chosen = candidates[0]
                assigned[duty.id] = chosen
                busy.add(chosen.id)

            # A crew not flying today is normally on rostered rest, not on call.
            # Standby is a separate roster line an airline chooses to pay for.
            callable_units = [c for c in pool if c.id not in busy
                              and rules.rolling_ok(c._roll, 400)]     # type: ignore[attr-defined]
            standby = min(len(callable_units),
                          _round_half_up(cfg.policy.standby_pct / 100 * n_units))
            standby_left, callouts = standby, 0

            # --- fly the day ---
            outcomes = {l.id: LegOutcome(leg=l) for l in day_legs}
            for l in day_legs:
                outcomes[l.id].noise = _round_half_up(noise() * 12)

            duty_state = {
                d.id: {"eff_start": d.start,
                       "cap": rules.max_duty_for(d.touches_night) or 10 ** 9,
                       "relieved": False}
                for d in duties}

            disrupted = 0
            for l in sorted(day_legs, key=lambda x: x.scheduled_departure):
                o = outcomes[l.id]
                duty = duty_of[l.id]
                st = duty_state[duty.id]
                ac = aircraft[l.aircraft]

                if l.id in self.exogenous:
                    o.state, o.reason = "CNL", EXOGENOUS
                    disrupted += 1
                    displaced.add(l.aircraft)
                    continue
                if duty.id not in assigned:
                    o.state, o.reason = "CNL", NO_CREW_ASSIGNED
                    disrupted += 1
                    displaced.add(l.aircraft)
                    continue
                if trust_plan and l.aircraft not in displaced:
                    # The operator had it here, by whatever means. Accept the plan.
                    ac.location = l.origin
                elif ac.location != l.origin:
                    o.state, o.reason = "CNL", RESOURCE_OUT_OF_POSITION
                    disrupted += 1
                    displaced.add(l.aircraft)
                    continue

                dep = max(l.scheduled_departure, ac.ready_at) + o.noise
                for cond in conditions:
                    eff = self.condition_effects.get(cond)
                    if eff and dep < eff.get("before", 0):
                        dep += eff.get("base", 0) + _round_half_up(noise() * eff.get("spread", 0))
                # Congestion feedback: disruption consumes gates, ground staff and
                # attention, slowing what is still flying. Without it a day that
                # cancels heavily appears to become *more* punctual, which is wrong.
                if self.congestion:
                    dep += _round_half_up(self.congestion * disrupted / max(1, len(day_legs)))
                if l.origin == cfg.network.hub and dep > l.scheduled_departure + 20:
                    dep += 5

                if (dep + l.block_minutes + rules.debrief_after_minutes) - st["eff_start"] > st["cap"]:
                    if not st["relieved"] and standby_left > 0:
                        standby_left -= 1
                        callouts += 1
                        st["relieved"] = True
                        st["eff_start"] = dep - rules.report_before_minutes
                        st["cap"] = rules.max_duty_for(False) or 10 ** 9
                        dep += cfg.crew.callout_minutes
                        o.relieved = True
                    else:
                        o.state, o.reason = "CNL", DUTY_LIMIT_REACHED
                        disrupted += 1
                        continue

                arr = dep + l.block_minutes
                o.state = "DLY" if (dep - l.scheduled_departure) >= 15 else "OK"
                o.departure, o.arrival = dep, arr
                displaced.discard(l.aircraft)
                ac.location = l.destination
                ac.ready_at = arr + cfg.fleet.turn(l.destination == cfg.network.hub)
                ac.last_arrival = arr

            # --- book crew hours ---
            for c in crews:
                c.duty_by_day[day] = 0
            for duty_id, c in assigned.items():
                c.duty_by_day[day] = duties[duty_id].minutes
                c.last_duty_end = t0 + duties[duty_id].end
                c.consecutive_days += 1
            for c in crews:
                if not c.duty_by_day[day]:
                    c.consecutive_days = 0

            # --- overnight: readiness resets, position does not ---
            stranded = repositioned = 0
            for ac in aircraft.values():
                ac.ready_at = max(cfg.schedule.day_start - 60,
                                  ac.last_arrival - DAY_MINUTES + cfg.fleet.overnight_ground_minutes)
                ac.last_arrival = 0
                if ac.location != ac.base:
                    if repositioned < repositioning:
                        ac.location = ac.base
                        repositioned += 1
                    else:
                        stranded += 1

            # Planned repositioning. A real timetable contains aircraft moves the
            # passenger schedule does not show: ferry flights, maintenance, crew
            # positioning. They appear as a gap between where a day ends and where
            # the next begins. For aircraft the simulation did not itself displace,
            # trust the plan; otherwise every planned gap becomes a phantom cascade.
            # Aircraft the model *did* displace stay where the model left them.
            if planned_start and day + 1 < cfg.days:
                for acid, origin in planned_start[day + 1].items():
                    if acid in aircraft and acid not in displaced:
                        aircraft[acid].location = origin
            displaced.clear()

            day_outcomes = [outcomes[l.id] for l in day_legs]
            results.append(DayResult(day=day, outcomes=day_outcomes,
                                     unstaffed_duties=unstaffed, stranded_overnight=stranded,
                                     standby_available=standby, standby_callouts=callouts,
                                     conditions=conditions))

        return SeasonResult(config=cfg, days=results,
                            duties_per_day=js_mean_int([len(d) for d in duties_per_day]),
                            crew_units=n_units, schedule=schedule)


def js_mean_int(xs: list[int]) -> int:
    return int(round(sum(xs) / len(xs))) if xs else 0


def run_experiment(cfg: ExperimentConfig, **kw) -> SeasonResult:
    """Run one experiment and return its result.

    A convenience wrapper over `Simulator(cfg, **kw).run()` for callers who do
    not need to hold on to the simulator itself.
    """
    return Simulator(cfg, **kw).run()
