# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Parity: the generic engine against the verified case-study engine.

The case-study implementation is itself verified leg-for-leg against an
independent JavaScript reference (877 assertions), so it is a trustworthy
oracle. If the configurable engine reproduces it exactly across policies and
seeds, the generalisation did not quietly change the model.

This is the regression test that protects the framework's central claim: that
the IndiGo study is one configuration of a general tool, not a special case with
a wrapper around it.

Run:
    python tests/test_parity.py
"""

from __future__ import annotations

import copy
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "reference"))

from airresilience.model import load_experiment                      # noqa: E402
from airresilience.engine import (                                   # noqa: E402
    DUTY_LIMIT_REACHED, NO_CREW_ASSIGNED, RESOURCE_OUT_OF_POSITION,
    Simulator, build_schedule,
)

# The reference engine names cancellation reasons after the regulation that
# produced them. The framework names them after the mechanism, so that a duty
# limit is the same event under any authority. Translate at the boundary.
REF_REASON = {"fdtl": DUTY_LIMIT_REACHED,
              "oop": RESOURCE_OUT_OF_POSITION,
              "unstaffed": NO_CREW_ASSIGNED,
              "": None}

try:
    from indigo_model import (                                       # noqa: E402
        FITTED_CONGESTION, StructuralConfig, base_profile, build_template, run_season,
    )
except ModuleNotFoundError:
    sys.exit(
        "The reference implementation was not found.\n\n"
        f"Expected: {ROOT / 'reference' / 'indigo_model.py'}\n\n"
        "It ships with this package as a test oracle. If you removed it, the\n"
        "framework still runs; only this parity test needs it.")

CONFIG = ROOT / "configs" / "indigo_bom.yaml"
GREEN, RED, DIM, RESET = "\033[32m", "\033[31m", "\033[2m", "\033[0m"

failures: list[str] = []
checks = 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if not ok:
        failures.append(f"{label}  {detail}")


def variant(seed: int, standby: float, roster: str, fdtl_hours: float):
    """Build a config and the matching reference arguments for one scenario."""
    cfg = copy.deepcopy(load_experiment(CONFIG))
    cfg.seed = seed
    cfg.policy.standby_pct = standby
    cfg.policy.roster_mode = roster
    cfg.regulation.max_duty_minutes = int(round(780 - fdtl_hours * 60))
    # The reference couples weekly rest to whether the rule change is active.
    # The configurable engine states it explicitly, so parity requires setting it.
    cfg.regulation.weekly_rest_days = 2 if fdtl_hours > 0 else 1
    ref_kw = dict(fdtl_hours=fdtl_hours,
                  roster_mode="legacy" if roster == "legacy" else "replanned",
                  standby_pct=standby)
    return cfg, ref_kw


SCENARIOS = [
    (seed, standby, roster, fdtl)
    for seed in (101, 102, 103, 104)
    for standby, roster, fdtl in [
        (0, "legacy", 1.7),
        (3, "legacy", 1.7),
        (12, "legacy", 1.7),
        (0, "compliant", 1.7),
        (0, "legacy", 0.0),
    ]
]


def main() -> None:
    print(f"Parity: configurable engine vs verified case-study engine")
    print(f"{DIM}config: {CONFIG.relative_to(ROOT)}{RESET}\n")

    # --- 1. schedule generation -------------------------------------------
    for seed in (101, 104, 250):
        cfg = copy.deepcopy(load_experiment(CONFIG))
        cfg.seed = seed
        gen = build_schedule(cfg)
        _, ref_legs = build_template(seed, base_profile())
        check(f"schedule seed={seed} length", len(gen) == len(ref_legs),
              f"{len(gen)} vs {len(ref_legs)}")
        diff = sum(1 for a, b in zip(gen, ref_legs)
                   if (a.origin, a.destination, a.scheduled_departure, a.block_minutes)
                   != (b.frm, b.to, b.sched_dep, b.block))
        check(f"schedule seed={seed} identical", diff == 0, f"{diff} legs differ")
    print(f"  1. schedule generation           3 seeds x {len(gen)} legs")

    # --- 2. season outcomes ------------------------------------------------
    leg_diffs = 0
    for seed, standby, roster, fdtl in SCENARIOS:
        cfg, ref_kw = variant(seed, standby, roster, fdtl)
        got = Simulator(cfg, congestion_minutes=FITTED_CONGESTION).run()
        exp = run_season(seed, base_profile(),
                         cfg=StructuralConfig(congestion=FITTED_CONGESTION),
                         record_all=True, **ref_kw)
        tag = f"seed={seed} sb={standby}% {roster} fdtl={fdtl}"

        check(f"{tag} cancellations", got.cancelled == exp["summary"]["cnl"],
              f"{got.cancelled} vs {exp['summary']['cnl']}")
        check(f"{tag} legs", got.legs == exp["summary"]["total"],
              f"{got.legs} vs {exp['summary']['total']}")
        check(f"{tag} duty lines", got.duties_per_day == exp["duties"],
              f"{got.duties_per_day} vs {exp['duties']}")
        check(f"{tag} crew units", got.crew_units == exp["crews"],
              f"{got.crew_units} vs {exp['crews']}")

        for d, (day, ref_day) in enumerate(zip(got.days, exp["days"])):
            check(f"{tag} day{d} cancelled", day.cancelled == ref_day["cnl"],
                  f"{day.cancelled} vs {ref_day['cnl']}")
            check(f"{tag} day{d} on time", day.on_time == ref_day["ok"],
                  f"{day.on_time} vs {ref_day['ok']}")
            check(f"{tag} day{d} delayed", day.delayed == ref_day["dly"],
                  f"{day.delayed} vs {ref_day['dly']}")
            check(f"{tag} day{d} stranded", day.stranded_overnight == ref_day["stranded"],
                  f"{day.stranded_overnight} vs {ref_day['stranded']}")
            check(f"{tag} day{d} callouts", day.standby_callouts == ref_day["calls"],
                  f"{day.standby_callouts} vs {ref_day['calls']}")
            check(f"{tag} day{d} unstaffed", day.unstaffed_duties == ref_day["unstaffed"],
                  f"{day.unstaffed_duties} vs {ref_day['unstaffed']}")

            # leg-by-leg: state, reason and realised times must all agree
            ref_legs = exp["all_days"][d]["legs"]
            for o, rl in zip(day.outcomes, ref_legs):
                mine = (o.state, o.reason, o.departure, o.arrival)
                theirs = (rl.state, REF_REASON.get(rl.reason or "", rl.reason), rl.dep, rl.arr)
                if mine != theirs:
                    leg_diffs += 1
                    if leg_diffs <= 3:
                        failures.append(f"{tag} day{d} leg{rl.id}: {mine} vs {theirs}")
    check("every leg identical", leg_diffs == 0, f"{leg_diffs} legs differ")
    n_legs = len(SCENARIOS) * got.legs
    print(f"  2. season outcomes              {len(SCENARIOS)} scenarios, "
          f"{n_legs:,} leg comparisons")

    # --- 3. the figures reported in the study ------------------------------
    # These are means over the 20 schedules the study used, not single runs, so
    # they are asserted here the same way they were reported.
    direct = knock = rate = 0.0
    seeds = range(101, 121)
    for seed in seeds:
        cfg, _ = variant(seed, 0, "legacy", 1.7)
        run = Simulator(cfg, congestion_minutes=FITTED_CONGESTION).run()
        r = run.by_reason()
        direct += run.direct()
        knock += run.propagated()
        rate += run.cancel_pct
    n = len(list(seeds))
    direct, knock, rate = direct / n, knock / n, rate / n
    mult = (direct + knock) / direct

    check("direct cancellations ~131", abs(direct - 131) < 1.5, f"got {direct:.0f}")
    check("knock-on cancellations ~384", abs(knock - 384) < 2.5, f"got {knock:.0f}")
    check("cascade multiplier ~3.9", abs(mult - 3.92) < 0.05, f"got {mult:.2f}")
    check("week cancel rate ~25.9%", abs(rate - 25.89) < 0.1, f"got {rate:.2f}")
    print(f"  3. figures reported in the study {n} seeds: {direct:.0f} direct, "
          f"{knock:.0f} knock-on, cascade x{mult:.2f}, {rate:.2f}% cancelled")

    print()
    if failures:
        print(f"{RED}FAILED{RESET}  {len(failures)} of {checks} checks")
        for f in failures[:20]:
            print(f"  {f}")
        sys.exit(1)
    print(f"{GREEN}PASSED{RESET}  all {checks:,} checks")
    print(f"{DIM}The configurable engine reproduces the verified implementation exactly,{RESET}")
    print(f"{DIM}across policies, regulations and seeds, down to individual departure times.{RESET}")


if __name__ == "__main__":
    main()
