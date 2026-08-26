# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
AirResilience command line.

    python run.py configs/indigo_bom.yaml                     # run and report
    python run.py configs/indigo_bom.yaml --trace out.json    # also write a trace
    python run.py configs/indigo_bom.yaml --standby 12 --roster compliant
    python run.py configs/indigo_bom.yaml --sweep standby 0 2 4 6 8 10 12 15 20
    python run.py configs/indigo_bom.yaml --seeds 101-120     # replicate and average
"""
from __future__ import annotations
import argparse, copy, pathlib, statistics, sys

from .model import load_experiment
from .engine import Simulator
from .emit import emit


def _make_stdout_safe() -> None:
    """Never let a printable character abort a run.

    Console output here is ASCII by design, but a configuration's own name or
    description comes from the user's file and may not be. On Windows the
    console defaults to a legacy code page, where printing such a string raises
    UnicodeEncodeError and the run dies after the work is done. Ask for UTF-8,
    and fall back to replacing what cannot be encoded.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def parse_seeds(spec: str) -> list[int]:
    if "-" in spec:
        a, b = spec.split("-"); return list(range(int(a), int(b) + 1))
    return [int(x) for x in spec.split(",")]


def configure(base, seed=None, standby=None, roster=None):
    c = copy.deepcopy(base)
    if seed is not None: c.seed = seed
    if standby is not None: c.policy.standby_pct = standby
    if roster is not None: c.policy.roster_mode = roster
    return c


def main() -> None:
    _make_stdout_safe()
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config")
    ap.add_argument("--congestion", type=float, default=40.0,
                    help="hub congestion feedback in minutes at full disruption")
    ap.add_argument("--standby", type=float)
    ap.add_argument("--roster", choices=["legacy", "compliant"])
    ap.add_argument("--seeds", default=None, help="e.g. 101-120 or 101,104")
    ap.add_argument("--sweep", nargs="+", metavar=("LEVER", "VALUE"),
                    help="sweep a lever: --sweep standby 0 4 8 12")
    ap.add_argument("--trace", help="write a trace to this path")
    a = ap.parse_args()

    try:
        base = load_experiment(a.config)
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        # These are the user's mistakes, not the program's. A traceback here
        # tells them nothing they can act on.
        raise SystemExit(f"error: {exc}")
    print(f"{base.name}\n{'-' * len(base.name)}")
    print(f"regulation : {base.regulation.describe()}")
    if base.baseline_regulation:
        print(f"roster built under : {base.baseline_regulation.describe()}")
    print(f"network    : {len(base.network.airports)} airports, hub {base.network.hub}, "
          f"{base.fleet.count} aircraft, {base.days} days\n")

    seeds = parse_seeds(a.seeds) if a.seeds else [base.seed]

    if a.sweep:
        lever, values = a.sweep[0], [float(v) for v in a.sweep[1:]]
        print(f"{lever:>10} {'cancel%':>9} {'sd':>6} {'cascade':>9} {'duties':>8}")
        print("-" * 46)
        for v in values:
            runs = [Simulator(configure(base, seed=s, **{lever: v}),
                              congestion_minutes=a.congestion).run() for s in seeds]
            rates = [r.cancel_pct for r in runs]
            sd = statistics.pstdev(rates) if len(rates) > 1 else 0.0
            mult = statistics.mean([r.cascade_multiplier() for r in runs])
            print(f"{v:>10.0f} {statistics.mean(rates):>8.2f}% {sd:>6.2f} "
                  f"{mult:>8.2f}x {runs[0].duties_per_day:>8}")
        return

    runs = [Simulator(configure(base, seed=s, standby=a.standby, roster=a.roster),
                      congestion_minutes=a.congestion).run() for s in seeds]
    rates = [r.cancel_pct for r in runs]

    if len(runs) > 1:
        print(f"{len(runs)} replications, seeds {seeds[0]}-{seeds[-1]}")
        print(f"  cancellation rate   {statistics.mean(rates):.2f}%  "
              f"(sd {statistics.pstdev(rates):.2f}, min {min(rates):.1f}, max {max(rates):.1f})")
        print(f"  cascade multiplier  {statistics.mean([r.cascade_multiplier() for r in runs]):.2f}x")
        print(f"\n  Differences smaller than the standard deviation across schedules")
        print(f"  should not be read as results.")
    else:
        r = runs[0]
        print(f"{'day':>5} {'legs':>6} {'cancelled':>10} {'rate':>8} {'on time':>9} "
              f"{'stranded':>9} {'callouts':>9}")
        print("-" * 60)
        for d in r.days:
            labels = base.day_labels
            label = labels[d.day] if d.day < len(labels) else str(d.day + 1)
            print(f"{label:>5} "
                  f"{d.legs:>6} {d.cancelled:>10} {d.cancel_pct:>7.1f}% "
                  f"{d.otp_pct:>8.1f}% {d.stranded_overnight:>9} {d.standby_callouts:>9}")
        s = r.summary()
        print("-" * 60)
        print(f"week: {s['cancelled']} of {s['legs']} cancelled ({s['cancel_pct']:.2f}%), "
              f"cascade x{s['cascade_multiplier']}")
        print(f"      by reason: " + ", ".join(f"{k} {v}" for k, v in s["by_reason"].items()))

    if a.trace:
        p = emit(runs[0], day_labels=base.day_labels or None).write(a.trace)
        print(f"\ntrace written to {p} ({p.stat().st_size/1024:.0f} KB)")


if __name__ == "__main__":
    main()
