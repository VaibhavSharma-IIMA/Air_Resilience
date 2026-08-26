# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Validate propagation against observed cancellations.

Design
------
The model has two propagation mechanisms: aircraft stranded out of position, and
crews reaching duty limits. **Crew is disabled here**, because no public dataset
records which crew worked which flight. What remains is testable exactly, because
BTS tail numbers reconstruct real aircraft rotations.

Cancellations BTS codes as weather or airspace are treated as *exogenous*:
decisions taken outside the operator's control loop, largely in advance. They are
handed to the model rather than predicted by it. Cancellations coded *carrier*
are the comparison target, since those are what an operator's own recovery either
prevents or fails to prevent.

Nothing is fitted. Given a schedule and an injected set of cancellations, the
propagation count follows from the rotation structure alone, so it cannot be
tuned toward the answer. A parameter sweep is run to demonstrate that.

    python validate_bts.py storm/                       # one period
    python validate_bts.py calm/ weather/ storm/        # several, compared
    python validate_bts.py storm/ --report out.md
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from airresilience import Simulator, load_experiment                  # noqa: E402
from airresilience.engine import RESOURCE_OUT_OF_POSITION             # noqa: E402


def _read_json(base: pathlib.Path):
    """Accept either a plain file or a gzipped one, so shipped data stays small."""
    if base.exists():
        return json.loads(base.read_text(encoding="utf-8"))
    gz = base.with_suffix(base.suffix + ".gz")
    if gz.exists():
        import gzip
        return json.loads(gzip.decompress(gz.read_bytes()).decode())
    raise FileNotFoundError(base)


def load_case(d: pathlib.Path):
    # A gzipped schedule is expanded once, in place, so the engine's plain CSV
    # reader needs no special handling.
    sched = d / "schedule.csv"
    if not sched.exists() and (d / "schedule.csv.gz").exists():
        import gzip
        sched.write_bytes(gzip.decompress((d / "schedule.csv.gz").read_bytes()))
    cfg = load_experiment(d / "config.yaml")
    observed = _read_json(d / "observed.json")
    try:
        exogenous = set(_read_json(d / "exogenous.json"))
    except FileNotFoundError:
        exogenous = set()
    return cfg, observed, exogenous


def evaluate(d: pathlib.Path, turn: int | None = None,
             congestion: float = 0.0, repositioning: int | None = None) -> dict:
    cfg, observed, exogenous = load_case(d)
    if turn is not None:
        cfg.fleet.turn_minutes = {"hub": turn, "outstation": turn}
    if repositioning is not None:
        cfg.policy.repositioning_per_night = repositioning

    r = Simulator(cfg, congestion_minutes=congestion,
                  exogenous_cancellations=exogenous).run()
    propagated = r.by_reason().get(RESOURCE_OUT_OF_POSITION, 0)

    n = len(observed)
    carrier = sum(1 for v in observed.values()
                  if v["cancelled"] and v["code"] == "carrier")
    weather = sum(1 for v in observed.values()
                  if v["cancelled"] and v["code"] in ("weather", "national air system"))
    return {"name": cfg.name, "legs": n, "injected": len(exogenous),
            "propagated": propagated, "observed_carrier": carrier,
            "observed_weather": weather,
            "model_pct": 100 * propagated / n if n else 0.0,
            "observed_pct": 100 * carrier / n if n else 0.0}


def sensitivity(d: pathlib.Path) -> list[dict]:
    """Show the result does not depend on the free parameters."""
    out = []
    for turn in (20, 30, 40):
        for cong, repo in ((0, 0), (30, 0), (0, 20)):
            e = evaluate(d, turn=turn, congestion=cong, repositioning=repo)
            out.append({"turn": turn, "congestion": cong, "repositioning": repo,
                        "model_pct": e["model_pct"]})
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cases", nargs="+", help="directories produced by bts_ingest.py")
    ap.add_argument("--sensitivity", action="store_true",
                    help="sweep the free parameters to show the result is structural")
    ap.add_argument("--report", help="write a markdown summary here")
    a = ap.parse_args()

    print("Propagation validation")
    print("Crew constraints disabled; weather and airspace cancellations injected")
    print("as given; carrier-coded cancellations are the comparison target.\n")
    header = (f"{'period':<22}{'legs':>9}{'injected':>10}{'model':>9}"
              f"{'observed':>10}{'model %':>10}{'obs %':>8}")
    print(header)
    print("-" * len(header))

    # A shell glob such as examples/bts_validation/* also matches the README,
    # so keep only directories rather than failing on the first file.
    cases = [pathlib.Path(c) for c in a.cases if pathlib.Path(c).is_dir()]
    skipped = [c for c in a.cases if not pathlib.Path(c).is_dir()]
    if skipped:
        print(f"(skipping {len(skipped)} non-directory argument(s))\n")
    if not cases:
        raise SystemExit("no dataset directories given")

    rows = []
    for c in cases:
        e = evaluate(c)
        rows.append(e)
        print(f"{e['name'][:21]:<22}{e['legs']:>9,}{e['injected']:>10,}"
              f"{e['propagated']:>9,}{e['observed_carrier']:>10,}"
              f"{e['model_pct']:>9.2f}%{e['observed_pct']:>7.2f}%")

    if a.sensitivity:
        print("\nSensitivity of the last period to the free parameters")
        print(f"  {'turn':>5}{'cong':>6}{'repo':>6}{'model %':>10}")
        for s in sensitivity(cases[-1]):
            print(f"  {s['turn']:>5}{s['congestion']:>6}{s['repositioning']:>6}"
                  f"{s['model_pct']:>9.2f}%")
        print("  Invariant: propagation follows from the rotation structure, not")
        print("  from any tunable parameter.")

    print("\nCrew legality is disabled and remains untested; see docs/validation.md.")

    if a.report:
        L = ["# Propagation validation", "",
             "| Period | Legs | Injected | Model | Observed carrier | Model % | Observed % |",
             "|---|---|---|---|---|---|---|"]
        for e in rows:
            L.append(f"| {e['name']} | {e['legs']:,} | {e['injected']:,} | "
                     f"{e['propagated']:,} | {e['observed_carrier']:,} | "
                     f"{e['model_pct']:.2f}% | {e['observed_pct']:.2f}% |")
        L += ["", "Crew constraints disabled. Weather and airspace cancellations are",
              "injected as given; carrier-coded cancellations are the target.",
              "Nothing is fitted."]
        pathlib.Path(a.report).write_text("\n".join(L), encoding="utf-8")
        print(f"report written to {a.report}")


if __name__ == "__main__":
    main()
