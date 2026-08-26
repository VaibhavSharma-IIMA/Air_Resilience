#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Worked analysis: calibration, attribution and structural robustness.

Nothing in this file is specific to any one study. The configuration, the causes
to decompose, the targets to calibrate against and the structural variants to
sweep all come from a study spec, which is a small JSON file. Point it at your
own configuration and spec and the same three analyses run unchanged.

    python examples/analysis_demo.py                        # all three
    python examples/analysis_demo.py attribute              # just one
    python examples/analysis_demo.py --spec mystudy.json
    python examples/analysis_demo.py attribute --config configs/example_p2p.yaml

A study spec declares, in data:

    config          the experiment configuration to load
    seeds           [start, stop] for replication
    sim_kw          keyword arguments passed to every simulation
    baseline        edits producing the benign case to decompose against
    causes          name -> list of edits, each {"path": ..., "value": ...}
    calibration     parameters, targets and search budget
    sweep           parameters, targets, structural variants, metrics to report

Targets are observations of the operation being modelled. They are inputs to a
study rather than properties of the software, so anything published from a
fitted parameter should cite where they came from.
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from airresilience.model import load_experiment
from airresilience.calibration import CalibrationSpec, ParameterSpec, Target, fit
from airresilience.metrics import attribute, replicate, structural_sweep

DEFAULT_SPEC = ROOT / "paper" / "figures" / "indigo.figspec.json"


def _coerce(value):
    """JSON object keys are strings; day-indexed maps need integer keys."""
    if isinstance(value, dict):
        return {(int(k) if isinstance(k, str) and k.lstrip("-").isdigit() else k):
                _coerce(v) for k, v in value.items()}
    return value


def apply_ops(cfg, ops):
    """Apply declared edits to a copy of a configuration.

    An edit marked `both_rule_sets` is applied to the rules in force *and* to
    the rules the roster was built under. In legacy roster mode duties come from
    the baseline rule set, so a variant touching only `regulation` would
    silently leave the roster alone. That is the sort of mistake a structural
    sweep exists to expose, so it should not be possible to make it here.
    """
    c = copy.deepcopy(cfg)
    for op in ops:
        value = _coerce(op["value"])
        if op.get("both_rule_sets"):
            setattr(c.regulation, op["path"], value)
            if c.baseline_regulation:
                setattr(c.baseline_regulation, op["path"], value)
            continue
        target, attr = c, op["path"].split(".")
        for part in attr[:-1]:
            target = getattr(target, part)
        setattr(target, attr[-1], value)
    return c


def make_spec(block) -> CalibrationSpec:
    return CalibrationSpec(
        parameters=[ParameterSpec(p["path"], p["low"], p["high"], step=p.get("step"),
                                  note=p.get("note", ""))
                    for p in block["parameters"]],
        targets=[Target(t["metric"], t["value"], t.get("tolerance", 1.0),
                        fitted=t.get("fitted", True), note=t.get("note", ""))
                 for t in block["targets"]],
        seeds=range(*block["seeds"]),
        max_evaluations=block.get("max_evaluations", 100))


def as_functions(named_ops):
    return {name: (lambda ops: (lambda c: apply_ops(c, ops)))(ops)
            for name, ops in named_ops.items()}


def rule(title):
    print("\n" + "=" * 72 + f"\n{title}\n" + "=" * 72)


def do_calibrate(cfg, study):
    block = study.get("calibration")
    if not block:
        print("\nno calibration block in this study spec; skipping")
        return
    rule("CALIBRATION")
    print("Recover the free parameters from observed outcomes, starting away from\n"
          "the answer so the search is not told where to land.\n")
    print(fit(apply_ops(cfg, block.get("start_from", [])),
              make_spec(block), method="grid").report())


def do_attribute(cfg, study):
    if "causes" not in study:
        print("\nno causes in this study spec; skipping attribution")
        return
    rule("ATTRIBUTION")
    seeds, sim_kw = range(*study["seeds"]), study.get("sim_kw", {})
    metric = study.get("metric", "cancel_pct")
    base = apply_ops(cfg, study.get("baseline", []))

    print(f"Benign baseline: {replicate(base, seeds, metric, **sim_kw).describe()}\n")
    a = attribute(base, as_functions(study["causes"]), seeds=seeds,
                  metric=metric, **sim_kw)
    print(a.report())
    print("\n  coalitions:")
    for row in a.coalition_table():
        on = [c for c in a.causes if row[c]]
        print(f"    {row['outcome']:>7.2f}%   {' + '.join(on) if on else 'nothing adverse'}")
    print("\n  Read the coalitions before the shares: a cause that is almost")
    print("  harmless on its own can dominate in company, which is why the")
    print("  decomposition has to be exact rather than additive.")


def do_sweep(cfg, study):
    block = study.get("sweep")
    if not block:
        print("\nno sweep block in this study spec; skipping")
        return
    rule("STRUCTURAL ROBUSTNESS")
    print("Vary an uncertain assumption, refit each variant to the same target,\n"
          "then ask which conclusions survive.\n")
    sw = structural_sweep(cfg, as_functions(block["variants"]),
                          seeds=range(*block["seeds"]),
                          calibration_spec=make_spec(block),
                          refit_kw={"method": "grid"}, **study.get("sim_kw", {}))
    print(sw.report(block.get("report_metrics", ["cancel_pct"])))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("stage", nargs="?", default="all",
                    choices=["all", "calibrate", "attribute", "sweep"])
    ap.add_argument("--spec", default=str(DEFAULT_SPEC),
                    help="study spec JSON (default: the hub study)")
    ap.add_argument("--config", help="override the configuration named in the spec")
    ap.add_argument("--metric", help="outcome to attribute, e.g. cancel_pct or "
                                     "otp_pct (default: the spec's own)")
    args = ap.parse_args()

    study = json.loads(pathlib.Path(args.spec).read_text(encoding="utf-8"))
    if args.metric:
        study["metric"] = args.metric
    cfg = load_experiment(ROOT / (args.config or study["config"]))
    print(f"configuration: {cfg.name}")
    print(f"study spec:    {pathlib.Path(args.spec).name}")

    stages = {"calibrate": do_calibrate, "attribute": do_attribute, "sweep": do_sweep}
    for name in (stages if args.stage == "all" else [args.stage]):
        stages[name](cfg, study)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
