# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Unit tests for the framework.

Parity (`test_parity.py`) proves the engine reproduces a verified predecessor.
These tests cover everything parity cannot: that invalid input is rejected, that
the contracts hold at their boundaries, and that the analysis layer computes what
it claims to.

Runs under pytest, or standalone:

    python tests/test_units.py
    pytest tests/test_units.py -q
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from airresilience import (                                              # noqa: E402
    CalibrationSpec, DUTY_LIMIT_REACHED, ExperimentConfig, ParameterSpec,
    RESOURCE_OUT_OF_POSITION, RuleSet, Simulator, Target, attribute, build_duties,
    build_schedule, emit, fit, load_experiment, replicate, trace,
)
from airresilience.engine import load_schedule_csv                       # noqa: E402
from airresilience.regulations import NightRule, RollingLimit, load_ruleset  # noqa: E402

CONFIG = ROOT / "configs" / "indigo_bom.yaml"


def raises(exc, fn, *a, **kw) -> bool:
    try:
        fn(*a, **kw)
    except exc:
        return True
    except Exception:
        return False
    return False


def base_cfg() -> ExperimentConfig:
    return copy.deepcopy(load_experiment(CONFIG))


# ---------------------------------------------------------------------------
# Rule DSL
# ---------------------------------------------------------------------------

def test_ruleset_limits():
    r = RuleSet(name="t", max_duty_minutes=780, min_rest_minutes=720,
                rolling=[RollingLimit(days=7, max_duty_minutes=3600)],
                night=NightRule(window=(0, 360), duty_penalty_minutes=60),
                roster_headroom_minutes=120)
    assert r.max_duty_for(False) == 780
    assert r.max_duty_for(True) == 720, "night penalty must reduce the cap"
    assert r.roster_cap(False) == 660, "rosters are planned inside the legal cap"
    assert r.rest_satisfied(720) and not r.rest_satisfied(719)
    assert r.rolling_ok({7: 3200}, 400) and not r.rolling_ok({7: 3200}, 401)


def test_night_rule_has_two_independent_triggers():
    """Starting early and finishing late are distinct exposures."""
    n = NightRule(window=(0, 360), early_start_before=360, late_finish_after=1380)
    assert n.touches(300, 900), "a duty starting at 05:00 touches the window"
    assert n.touches(600, 1400), "a duty ending after 23:00 runs into it"
    assert not n.touches(600, 1000), "a mid-day duty does neither"
    off = NightRule(window=(0, 360)).disabled()
    assert not off.touches(0, 2000), "a disabled rule must never trigger"


def test_unset_rules_are_not_enforced():
    r = RuleSet(name="minimal")
    assert r.max_duty_for(True) is None and r.roster_cap(False) is None
    assert r.rest_satisfied(0), "an unset rest rule cannot fail"
    assert r.rolling_ok({}, 10 ** 6), "no rolling limits means nothing to breach"


def test_ruleset_roundtrip():
    r = load_ruleset("dgca_style_2025_post")
    back = RuleSet.from_dict(r.to_dict())
    assert back.max_duty_minutes == r.max_duty_minutes
    assert back.night.duty_penalty_minutes == r.night.duty_penalty_minutes
    assert [x.days for x in back.rolling] == [x.days for x in r.rolling]
    assert back.weekly_rest_days == r.weekly_rest_days


def test_builtin_rulesets_declare_reconstruction():
    for name in ("dgca_style_2025_pre", "dgca_style_2025_post"):
        assert load_ruleset(name).is_reconstruction, \
            "a rule set not transcribed from source must say so"
    assert raises(KeyError, load_ruleset, "faa_117")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def test_config_loads():
    c = base_cfg()
    assert c.network.hub == "BOM" and c.fleet.count == 40 and c.days == 7
    assert c.baseline_regulation is not None


def test_config_rejects_invalid():
    c = base_cfg(); c.network.hub = "XXX"
    assert raises(ValueError, c.validate), "hub must exist in the airport list"
    c = base_cfg(); c.fleet.count = 0
    assert raises(ValueError, c.validate)
    c = base_cfg(); c.policy.standby_pct = 150
    assert raises(ValueError, c.validate), "standby must be a percentage"
    c = base_cfg(); c.crew.units = None; c.crew.units_per_aircraft = None
    assert raises(ValueError, c.validate), "crew size must be specified somehow"
    from airresilience import Route
    c = base_cfg(); c.network.routes[0] = Route("BOM", "DEL", 0)
    assert raises(ValueError, c.validate), "a route must take positive time"


def test_crew_resolution():
    c = base_cfg()
    assert c.crew.resolve_units(40) == 117
    c.crew.units = 200
    assert c.crew.resolve_units(40) == 200, "an absolute count overrides the ratio"


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

def test_csv_ingest():
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "s.csv"
        p.write_text("aircraft,origin,destination,scheduled_departure,block_minutes\n"
                     "AC00,BOM,DEL,06:30,125\n"
                     "AC00,DEL,BOM,540,125\n"
                     "AC01,BOM,GOI,07:15,60\n")
        legs = load_schedule_csv(str(p))
    assert len(legs) == 3
    assert legs[0].scheduled_departure == 390, "HH:MM must parse to minutes"
    assert legs[1].scheduled_departure == 540, "plain minutes must pass through"
    assert legs[0].scheduled_arrival == 515
    assert {l.aircraft for l in legs} == {"AC00", "AC01"}


def test_generated_schedule_is_deterministic():
    c = base_cfg()
    assert [vars(l) for l in build_schedule(c)] == [vars(l) for l in build_schedule(c)]
    c2 = base_cfg(); c2.seed += 1
    assert build_schedule(c) != build_schedule(c2), "a different seed must differ"


def test_tighter_cap_needs_more_duties():
    """The structural half of a rule change, independent of any disruption."""
    c = base_cfg()
    sched = build_schedule(c)
    loose = build_duties(sched, c.baseline_regulation)
    tight_rules = copy.deepcopy(c.baseline_regulation)
    tight_rules.max_duty_minutes = 600
    tight = build_duties(sched, tight_rules)
    assert len(tight) > len(loose), "shorter duties means more of them for the same flying"


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

def test_run_is_deterministic():
    c = base_cfg()
    a = Simulator(c, congestion_minutes=40).run()
    b = Simulator(c, congestion_minutes=40).run()
    assert a.cancelled == b.cancelled and a.cancel_pct == b.cancel_pct
    assert [(o.state, o.departure) for d in a.days for o in d.outcomes] == \
           [(o.state, o.departure) for d in b.days for o in d.outcomes]


def test_benign_week_runs_clean():
    """With generous rules, no weather and a compliant roster, nothing is lost."""
    c = base_cfg()
    c.regulation.max_duty_minutes = 780
    c.regulation.weekly_rest_days = 1
    c.conditions = {}
    c.policy.roster_mode = "compliant"
    r = Simulator(c, congestion_minutes=0).run()
    # Not exactly zero: even a benign week loses a little to delay accumulating
    # near the end of long duties. What matters is the scale.
    assert r.cancel_pct < 3.0, f"a comfortable operation should not melt down: {r.cancel_pct:.1f}%"


def test_cascade_multiplier_is_a_property_of_coupling_not_severity():
    """Propagation dominates even when very little goes wrong.

    A benign week loses a handful of flights, and most of those are still
    displacement rather than the original cause. The multiplier describes how
    tightly the network is coupled; it does not measure how bad the day was.
    """
    benign = base_cfg()
    benign.regulation.max_duty_minutes = 780
    benign.regulation.weekly_rest_days = 1
    benign.conditions = {}
    benign.policy.roster_mode = "compliant"
    mild = Simulator(benign, congestion_minutes=0).run()
    severe = Simulator(base_cfg(), congestion_minutes=40).run()

    assert mild.cancelled < 0.15 * severe.cancelled, "the benign week must be far milder"
    assert mild.propagated() > mild.direct(), "yet displacement still dominates"
    assert abs(mild.cascade_multiplier() - severe.cascade_multiplier()) < 1.5, \
        "and the multiplier is broadly stable across severities"


def test_standby_reduces_cancellations_monotonically():
    c = base_cfg()
    rates = []
    for sb in (0, 4, 8, 12):
        c2 = copy.deepcopy(c); c2.policy.standby_pct = sb
        rates.append(Simulator(c2, congestion_minutes=40).run().cancel_pct)
    assert rates == sorted(rates, reverse=True), f"more standby should not hurt: {rates}"


def test_propagation_dominates_and_is_counted_consistently():
    r = Simulator(base_cfg(), congestion_minutes=40).run()
    by = r.by_reason()
    assert r.direct() + r.propagated() == sum(by.values()) == r.cancelled, \
        "every cancellation must fall into exactly one group"
    assert r.propagated() > r.direct(), "in a coupled network, displacement dominates"
    assert abs(r.cascade_multiplier() - (r.cancelled / r.direct())) < 1e-9


def test_legacy_roster_is_worse_than_compliant():
    c = base_cfg()
    legacy = Simulator(c, congestion_minutes=40).run().cancel_pct
    c2 = copy.deepcopy(c); c2.policy.roster_mode = "compliant"
    compliant = Simulator(c2, congestion_minutes=40).run().cancel_pct
    assert legacy > compliant, "flying a roster built for the old rules should cost something"


def test_repositioning_capacity_matters():
    c = base_cfg(); c.policy.repositioning_per_night = 0
    none = Simulator(c, congestion_minutes=40).run()
    c2 = base_cfg(); c2.policy.repositioning_per_night = 40
    lots = Simulator(c2, congestion_minutes=40).run()
    assert none.days[-1].stranded_overnight >= lots.days[-1].stranded_overnight
    assert none.cancel_pct > lots.cancel_pct, "overnight recovery should help"


# ---------------------------------------------------------------------------
# Dated versus repeating schedules
# ---------------------------------------------------------------------------

def test_repeating_schedule_flies_the_same_pattern_daily():
    from airresilience.engine import schedule_by_day
    c = base_cfg()
    sched = build_schedule(c)
    assert len({l.day for l in sched}) == 1, "a generated schedule is one repeating day"
    per_day = schedule_by_day(sched, 5)
    assert len(per_day) == 5
    assert all(len(d) == len(sched) for d in per_day), "each day flies the whole pattern"
    assert [l.day for l in per_day[3]] == [3] * len(sched), "and is stamped with its day"


def test_dated_schedule_is_split_not_repeated():
    """A real timetable differs day to day; repeating it would fly a week every day."""
    from airresilience.engine import schedule_by_day
    from airresilience.model import FlightLeg
    legs = [FlightLeg(i, "AC00", "AAA", "BBB", 400 + i * 10, 60, day=i % 3)
            for i in range(9)]
    per_day = schedule_by_day(legs, 3)
    assert [len(d) for d in per_day] == [3, 3, 3]
    assert sum(len(d) for d in per_day) == len(legs), \
        "a dated schedule must be partitioned, not duplicated"


def test_dated_csv_runs_the_right_number_of_legs():
    import tempfile, textwrap
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "s.csv"
        rows = ["aircraft,origin,destination,scheduled_departure,block_minutes,day,sequence"]
        for day in range(3):
            for i in range(2):
                rows.append(f"AC0{i},BOM,DEL,{7+day}:00,125,{day},0")
                rows.append(f"AC0{i},DEL,BOM,{11+day}:00,125,{day},1")
        p.write_text("\n".join(rows) + "\n")
        legs = load_schedule_csv(str(p))
    assert len(legs) == 12
    assert {l.day for l in legs} == {0, 1, 2}
    from airresilience.engine import schedule_by_day
    per_day = schedule_by_day(legs, 3)
    assert [len(x) for x in per_day] == [4, 4, 4]


# ---------------------------------------------------------------------------
# Topology independence
# ---------------------------------------------------------------------------

P2P = ROOT / "configs" / "example_p2p.yaml"


def test_point_to_point_config_runs():
    """No hub-and-spoke assumption: a mesh network with several bases."""
    c = load_experiment(P2P)
    r = Simulator(c, congestion_minutes=25).run()
    assert r.legs > 0
    origins = {o.leg.origin for d in r.days for o in d.outcomes}
    assert len(origins) > 2, "a point-to-point schedule departs from many airports"
    assert r.cancel_pct < 20, "this case should be disrupted, not destroyed"


def test_aircraft_start_where_their_first_leg_departs():
    """Not at the network hub, which a real timetable has no reason to respect."""
    c = load_experiment(P2P)
    sched = build_schedule(c)
    first = {}
    for l in sorted(sched, key=lambda x: x.scheduled_departure):
        first.setdefault(l.aircraft, l.origin)
    assert len(set(first.values())) > 1, "the fixture should use several bases"
    r = Simulator(c, congestion_minutes=0).run()
    day0 = r.days[0]
    oop = sum(1 for o in day0.outcomes
              if o.state == "CNL" and o.reason == RESOURCE_OUT_OF_POSITION)
    assert oop == 0, ("no aircraft should be out of position on the first morning; "
                      f"got {oop}, which means they were initialised at the wrong airport")


def test_standby_helps_in_a_mesh_too():
    c = load_experiment(P2P)
    without = Simulator(c, congestion_minutes=25).run().cancel_pct
    c2 = copy.deepcopy(c); c2.policy.standby_pct = 15
    with_sb = Simulator(c2, congestion_minutes=25).run().cancel_pct
    assert with_sb < without, "recovery capacity should work regardless of topology"


# ---------------------------------------------------------------------------
# Trace format
# ---------------------------------------------------------------------------

def good_trace() -> dict:
    return emit(Simulator(base_cfg(), congestion_minutes=40).run()).to_dict()


def test_emitted_trace_validates():
    t = good_trace()
    trace.validate(t)
    assert t["format"] == "airresilience.trace"
    assert t["network"]["hub"] == "BOM"
    assert len(t["days"]) == 7 and t["legs"]


def test_trace_rejects_malformed():
    t = good_trace(); t["format"] = "something else"
    assert raises(trace.TraceError, trace.validate, t)

    t = good_trace(); t["network"]["hub"] = "ZZZ"
    assert raises(trace.TraceError, trace.validate, t), "hub must be among the airports"

    t = good_trace(); t["legs"][0]["st"] = "MAYBE"
    assert raises(trace.TraceError, trace.validate, t)

    t = good_trace(); t["legs"][1] = dict(t["legs"][0])
    assert raises(trace.TraceError, trace.validate, t), "duplicate leg ids must be caught"

    t = good_trace(); t["legs"][0]["to"] = "ZZZ"
    assert raises(trace.TraceError, trace.validate, t)

    t = good_trace(); t["days"] = []
    assert raises(trace.TraceError, trace.validate, t)

    t = good_trace()
    op = next(l for l in t["legs"] if l["st"] != "CNL")
    op.pop("dep", None)
    assert raises(trace.TraceError, trace.validate, t), \
        "an operated leg without realised times is incoherent"


def test_trace_rejects_bad_provenance():
    t = good_trace()
    t["parameters"].append({"name": "x", "value": 1, "provenance": "vibes"})
    assert raises(trace.TraceError, trace.validate, t)


def test_every_parameter_declares_provenance():
    t = good_trace()
    assert t["parameters"], "a trace with no declared parameters is not self-describing"
    for p in t["parameters"]:
        assert p["provenance"] in trace.PROVENANCES


def test_summarise_recomputes_from_legs():
    """Metrics are derived, not trusted, so engines are comparable."""
    t = good_trace()
    s = trace.summarise(t)
    assert s["legs"] == len(t["legs"])
    assert s["cancelled"] == sum(1 for l in t["legs"] if l["st"] == "CNL")
    assert abs(s["cancel_pct"] - 100 * s["cancelled"] / s["legs"]) < 0.01
    t["metrics"]["cancelled"] = 999999
    assert trace.summarise(t)["cancelled"] == s["cancelled"], \
        "summarise must ignore a wrong summary block"


def test_trace_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        p = pathlib.Path(d) / "t.trace.json"
        r = Simulator(base_cfg(), congestion_minutes=40).run()
        emit(r).write(p)
        back = trace.read(p)
    assert trace.summarise(back)["cancelled"] == r.cancelled


# ---------------------------------------------------------------------------
# Calibration
# ---------------------------------------------------------------------------

def small_spec(**kw) -> CalibrationSpec:
    d = dict(parameters=[ParameterSpec("regulation.max_duty_minutes", 660, 690, step=30)],
             targets=[Target("cancel_pct", 28.5, 2.0)],
             seeds=(101, 102), max_evaluations=10)
    d.update(kw)
    return CalibrationSpec(**d)


def test_calibration_refuses_underdetermined():
    spec = small_spec(parameters=[ParameterSpec("regulation.max_duty_minutes", 660, 690, step=30),
                                  ParameterSpec("congestion_minutes", 0, 40, step=40)])
    assert raises(ValueError, fit, base_cfg(), spec), \
        "two parameters against one target must be refused"
    r = fit(base_cfg(), spec, allow_underdetermined=True)
    assert r.best, "the escape hatch must still work when asked for explicitly"


def test_calibration_needs_something_to_fit():
    assert raises(ValueError, fit, base_cfg(), small_spec(parameters=[]))
    spec = small_spec(targets=[Target("cancel_pct", 28.5, 2.0, fitted=False)])
    assert raises(ValueError, fit, base_cfg(), spec)


def test_calibration_reports_truncation():
    spec = small_spec(
        parameters=[ParameterSpec("regulation.max_duty_minutes", 600, 720, step=6)],
        max_evaluations=3)
    r = fit(base_cfg(), spec, method="grid")
    assert r.truncated, "a search cut short by its budget must say so"
    assert "max_evaluations" in r.report()


def test_held_out_targets_are_scored_but_not_fitted():
    spec = small_spec(targets=[Target("cancel_pct", 28.5, 2.0),
                               Target("cancel_pct.0", 23.1, 3.0, fitted=False)])
    r = fit(base_cfg(), spec, method="grid")
    assert len(spec.fitted_targets) == 1 and len(spec.held_out) == 1
    assert r.held_out_score() is not None
    assert any(not row["fitted"] for row in r.target_table())


def test_calibration_rejects_unknown_target():
    spec = small_spec(targets=[Target("not_a_metric", 1.0, 1.0)])
    assert raises(KeyError, fit, base_cfg(), spec)


def test_calibration_improves_on_the_starting_point():
    c = base_cfg(); c.regulation.max_duty_minutes = 780
    spec = small_spec(parameters=[ParameterSpec("regulation.max_duty_minutes", 650, 780, step=20)],
                      targets=[Target("cancel_pct", 28.5, 2.0)], seeds=(101, 102, 103))
    r = fit(c, spec, method="grid")
    assert r.best["regulation.max_duty_minutes"] < 780, "it should move off the start"
    assert r.objective < 3.0
    assert any(p["provenance"] == "calibrated" for p in r.to_parameters())


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def test_replicate_reports_spread():
    r = replicate(base_cfg(), range(101, 107), "cancel_pct", congestion_minutes=40)
    assert len(r.values) == 6 and r.sd > 0
    lo, hi = r.ci95()
    assert lo < r.mean < hi
    assert r.replications_for(0.5) > r.replications_for(5.0), \
        "a tighter interval needs more replications"


def test_shapley_sums_exactly_to_the_escalation():
    """The property that makes Shapley the right decomposition for interacting causes."""
    c = base_cfg()
    c.regulation.max_duty_minutes = 780
    c.conditions = {}
    c.policy.roster_mode = "compliant"
    a = attribute(c, {
        "rule change": lambda x: (setattr(x.regulation, "max_duty_minutes", 678), x)[-1],
        "fog": lambda x: (setattr(x, "conditions", {0: ["fog"], 1: ["fog"]}), x)[-1],
        "legacy roster": lambda x: (setattr(x.policy, "roster_mode", "legacy"), x)[-1],
    }, seeds=(101, 102), metric="cancel_pct", congestion_minutes=40)
    assert abs(a.residual()) < 1e-9, f"residual must be zero, got {a.residual()}"
    assert abs(sum(a.shares().values()) - 100) < 1e-6
    assert len(a.coalitions) == 8, "three causes means eight coalitions"


def test_attribution_refuses_a_silly_number_of_causes():
    causes = {f"c{i}": (lambda x: x) for i in range(7)}
    assert raises(ValueError, attribute, base_cfg(), causes)


def test_interaction_is_visible_in_the_coalitions():
    """A cause can do nothing alone and a great deal in company."""
    c = base_cfg()
    c.regulation.max_duty_minutes = 780
    c.conditions = {}
    c.policy.roster_mode = "compliant"
    a = attribute(c, {
        "rule change": lambda x: (setattr(x.regulation, "max_duty_minutes", 678), x)[-1],
        "legacy roster": lambda x: (setattr(x.policy, "roster_mode", "legacy"), x)[-1],
    }, seeds=(101, 102), metric="cancel_pct", congestion_minutes=40)
    alone = a.coalitions[(0, 1)] - a.coalitions[(0, 0)]
    together = a.coalitions[(1, 1)] - a.coalitions[(1, 0)]
    assert together > alone + 1, \
        "an un-replanned roster should cost far more once the rules have tightened"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> None:
    tests = [(n, f) for n, f in sorted(globals().items())
             if n.startswith("test_") and callable(f)]
    GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
    failed = []
    print(f"Unit tests: {len(tests)} cases\n")
    for name, fn in tests:
        try:
            fn()
            print(f"  {GREEN}ok{RESET}   {name.removeprefix('test_').replace('_', ' ')}")
        except AssertionError as e:
            failed.append((name, str(e) or "assertion failed"))
            print(f"  {RED}FAIL{RESET} {name.removeprefix('test_').replace('_', ' ')}")
        except Exception as e:
            failed.append((name, f"{type(e).__name__}: {e}"))
            print(f"  {RED}ERR {RESET} {name.removeprefix('test_').replace('_', ' ')}")
    print()
    if failed:
        print(f"{RED}FAILED{RESET}  {len(failed)} of {len(tests)}")
        for n, msg in failed:
            print(f"  {n}\n    {msg}")
        sys.exit(1)
    print(f"{GREEN}PASSED{RESET}  all {len(tests)} cases")


if __name__ == "__main__":
    main()
