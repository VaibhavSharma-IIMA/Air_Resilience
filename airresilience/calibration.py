# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Calibration.

Fitting a simulator to an observation is where most of the epistemic risk in a
simulation study lives, so this module tries to make the process explicit rather
than convenient. Three things are separated deliberately:

    Target      something observed in the world, with a tolerance
    Objective   how disagreement with the targets is scored
    Search      how the parameter space is explored

and a completed fit returns a `CalibrationResult` that records what was fitted,
to what, how well, and how many parameters were free. That record is the thing
you cite, not the point estimate.

Two warnings the module enforces rather than documents:

    Over-determination. Fitting k parameters to fewer than k targets is
    unidentified. `fit()` refuses unless you pass `allow_underdetermined=True`,
    which is deliberately awkward to type.

    Held-out checks. Targets marked `fitted=False` are scored but never
    optimised against, so a calibration reports its own out-of-sample error.

Example
-------
    spec = CalibrationSpec(
        parameters=[
            ParameterSpec("regulation.max_duty_minutes", 600, 780, step=6),
            ParameterSpec("congestion_minutes", 0, 80, step=5),
        ],
        targets=[
            Target("cancel_pct", observed=28.5, tolerance=2.0),
            Target("otp_pct.0", observed=49.5, tolerance=5.0),
            Target("otp_pct.1", observed=35.0, tolerance=5.0),
            Target("otp_pct.2", observed=19.7, tolerance=5.0),
        ],
        seeds=range(101, 111),
    )
    result = fit(cfg, spec)
    print(result.report())
"""

from __future__ import annotations

import math
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .engine import Simulator
from .model import ExperimentConfig


# ---------------------------------------------------------------------------
# What we are fitting to
# ---------------------------------------------------------------------------

@dataclass
class Target:
    """One observed quantity the simulation should reproduce.

    `name` addresses a value in the run summary produced by `observe()`:

        "cancel_pct"        a season-level metric
        "otp_pct.2"         a day-level metric, day index 2
        "cancelled.0"       cancellations on day 0

    `tolerance` is the width within which agreement is considered acceptable. It
    also normalises the objective, so targets on different scales contribute
    comparably without hand-tuned weights.

    Set `fitted=False` to hold a target out: it is scored and reported but never
    optimised against.
    """
    name: str
    observed: float
    tolerance: float = 1.0
    weight: float = 1.0
    fitted: bool = True
    note: str = ""

    def error(self, modelled: float) -> float:
        return modelled - self.observed

    def normalised(self, modelled: float) -> float:
        return self.error(modelled) / (self.tolerance or 1.0)

    def within(self, modelled: float) -> bool:
        return abs(self.error(modelled)) <= self.tolerance


@dataclass
class ParameterSpec:
    """A free parameter, addressed by a dotted path into the configuration.

        "regulation.max_duty_minutes"    a field on the rule set
        "policy.standby_pct"             a field on the policy
        "congestion_minutes"             a simulator argument, not config

    `step` gives the grid resolution. `integer` rounds proposals, which matters
    for quantities like minutes that the engine treats as whole numbers.
    """
    path: str
    low: float
    high: float
    step: float | None = None
    integer: bool = True
    note: str = ""

    def clamp(self, v: float) -> float:
        v = min(self.high, max(self.low, v))
        if self.step:
            v = self.low + round((v - self.low) / self.step) * self.step
            v = min(self.high, max(self.low, v))
        return round(v) if self.integer else v

    def grid(self) -> list[float]:
        step = self.step or (self.high - self.low) / 10 or 1
        n = int(math.floor((self.high - self.low) / step)) + 1
        return [self.clamp(self.low + i * step) for i in range(max(1, n))]


@dataclass
class CalibrationSpec:
    parameters: list[ParameterSpec]
    targets: list[Target]
    seeds: Sequence[int] = (1,)
    max_evaluations: int = 400
    restarts: int = 4
    objective: str = "normalised_rmse"   # or "normalised_max"

    @property
    def fitted_targets(self) -> list[Target]:
        return [t for t in self.targets if t.fitted]

    @property
    def held_out(self) -> list[Target]:
        return [t for t in self.targets if not t.fitted]


# ---------------------------------------------------------------------------
# Observing a configuration
# ---------------------------------------------------------------------------

def observe(cfg: ExperimentConfig, seeds: Iterable[int], congestion_minutes: float = 0.0,
            **sim_kw) -> dict[str, float]:
    """Run replications and return the flat metric dictionary targets address.

    Averaging across seeds before scoring is deliberate: fitting to a single
    schedule fits the schedule as much as the model.
    """
    import copy
    runs = []
    for s in seeds:
        c = copy.deepcopy(cfg)
        c.seed = s
        runs.append(Simulator(c, congestion_minutes=congestion_minutes, **sim_kw).run())

    out: dict[str, float] = {
        "cancel_pct": statistics.mean(r.cancel_pct for r in runs),
        "cancelled": statistics.mean(r.cancelled for r in runs),
        "legs": statistics.mean(r.legs for r in runs),
        "direct": statistics.mean(r.direct() for r in runs),
        "propagated": statistics.mean(r.propagated() for r in runs),
        "duties_per_day": statistics.mean(r.duties_per_day for r in runs),
    }
    mults = [r.cascade_multiplier() for r in runs if r.direct()]
    out["cascade_multiplier"] = statistics.mean(mults) if mults else float("nan")

    n_days = len(runs[0].days)
    for d in range(n_days):
        out[f"cancel_pct.{d}"] = statistics.mean(r.days[d].cancel_pct for r in runs)
        out[f"otp_pct.{d}"] = statistics.mean(r.days[d].otp_pct for r in runs)
        out[f"cancelled.{d}"] = statistics.mean(r.days[d].cancelled for r in runs)
        out[f"stranded.{d}"] = statistics.mean(r.days[d].stranded_overnight for r in runs)

    # Spread across schedules. Reported so a caller can see when a fit is being
    # asked to resolve differences smaller than the model's own noise.
    out["_sd_cancel_pct"] = statistics.pstdev([r.cancel_pct for r in runs]) if len(runs) > 1 else 0.0
    return out


def _set_path(cfg: ExperimentConfig, path: str, value: float,
              extras: dict[str, float]) -> None:
    """Apply a parameter to the config, or to the simulator arguments.

    A path without a dot names a simulator argument rather than a config field
    (``congestion_minutes``). Otherwise the path is walked, traversing both
    attributes and dict keys, so nested settings like
    ``fleet.turn_minutes.outstation`` are addressable.
    """
    if "." not in path:
        extras[path] = value
        return
    obj: Any = cfg
    parts = path.split(".")
    for p in parts[:-1]:
        obj = obj[p] if isinstance(obj, dict) else getattr(obj, p)
    last = parts[-1]
    if isinstance(obj, dict):
        obj[last] = value
    else:
        if not hasattr(obj, last):
            raise AttributeError(
                f"parameter path {path!r} does not resolve: "
                f"{type(obj).__name__} has no attribute {last!r}")
        setattr(obj, last, value)


def score(spec: CalibrationSpec, observed: dict[str, float],
          targets: Sequence[Target] | None = None) -> float:
    """Aggregate disagreement. Lower is better; 1.0 means typically on tolerance."""
    ts = list(targets if targets is not None else spec.fitted_targets)
    if not ts:
        return 0.0
    errs, weights = [], []
    for t in ts:
        if t.name not in observed:
            raise KeyError(f"target {t.name!r} is not produced by observe(); "
                           f"available: {sorted(k for k in observed if not k.startswith('_'))}")
        errs.append(t.normalised(observed[t.name]))
        weights.append(t.weight)
    if spec.objective == "normalised_max":
        return max(abs(e) for e in errs)
    total = sum(weights)
    return math.sqrt(sum(w * e * e for w, e in zip(weights, errs)) / total)


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------

@dataclass
class CalibrationResult:
    spec: CalibrationSpec
    best: dict[str, float]
    objective: float
    observed: dict[str, float]
    evaluations: int
    seconds: float
    history: list[tuple[dict[str, float], float]] = field(default_factory=list)
    method: str = ""
    truncated: bool = False        # the search ran out of budget before finishing

    @property
    def free_parameters(self) -> int:
        return len(self.spec.parameters)

    @property
    def n_fitted_targets(self) -> int:
        return len(self.spec.fitted_targets)

    def target_table(self) -> list[dict[str, Any]]:
        rows = []
        for t in self.spec.targets:
            m = self.observed.get(t.name, float("nan"))
            rows.append({"target": t.name, "observed": t.observed, "modelled": round(m, 3),
                         "error": round(t.error(m), 3), "tolerance": t.tolerance,
                         "within": t.within(m), "fitted": t.fitted})
        return rows

    def held_out_score(self) -> float | None:
        ho = self.spec.held_out
        return score(self.spec, self.observed, ho) if ho else None

    def report(self) -> str:
        L = [f"Calibration: {self.free_parameters} free parameter(s) fitted to "
             f"{self.n_fitted_targets} target(s)"]
        if self.free_parameters >= self.n_fitted_targets:
            L.append("  ! as many parameters as targets: the fit is not over-determined,")
            L.append("    so agreement is weak evidence that the mechanism is right.")
        L.append("")
        L.append("  fitted values")
        for p in self.spec.parameters:
            v = self.best[p.path]
            edge = ""
            if abs(v - p.low) < 1e-9 or abs(v - p.high) < 1e-9:
                edge = "   <- at the edge of the search range"
            L.append(f"    {p.path:<38} {v:>10g}{edge}")
        L.append("")
        L.append(f"  {'target':<20}{'observed':>10}{'modelled':>10}{'error':>9}{'':>4}{'':>3}")
        for r in self.target_table():
            flag = "ok " if r["within"] else "OUT"
            held = "" if r["fitted"] else "  (held out)"
            L.append(f"    {r['target']:<18}{r['observed']:>10}{r['modelled']:>10}"
                     f"{r['error']:>9}  {flag}{held}")
        L.append("")
        L.append(f"  objective            {self.objective:.4f}   "
                 f"({self.spec.objective}; 1.0 means typically on tolerance)")
        ho = self.held_out_score()
        if ho is not None:
            L.append(f"  held-out objective   {ho:.4f}   <- the number worth trusting")
        sd = self.observed.get("_sd_cancel_pct")
        if sd:
            L.append(f"  schedule noise       {sd:.2f} pts across {len(self.spec.seeds)} seeds")
        L.append(f"  {self.evaluations} evaluations ({self.method}) in {self.seconds:.1f}s")
        if self.truncated:
            L.append("  ! the search stopped on max_evaluations before covering the grid,")
            L.append("    so this is the best point seen, not the best point available.")
            L.append("    Raise max_evaluations or coarsen the step to search it fully.")
        return "\n".join(L)

    def to_parameters(self) -> list[dict[str, Any]]:
        """Provenance records for the fitted values, ready to attach to a trace."""
        return [{"name": p.path.split(".")[-1], "value": self.best[p.path],
                 "provenance": "calibrated",
                 "note": (p.note + " " if p.note else "") +
                         f"fitted to {self.n_fitted_targets} target(s), "
                         f"objective {self.objective:.3f}"}
                for p in self.spec.parameters]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

def _evaluate(cfg: ExperimentConfig, spec: CalibrationSpec,
              values: dict[str, float], sim_kw: dict) -> tuple[float, dict[str, float]]:
    import copy
    c = copy.deepcopy(cfg)
    extras = dict(sim_kw)
    for p in spec.parameters:
        _set_path(c, p.path, values[p.path], extras)
    obs = observe(c, spec.seeds, **extras)
    return score(spec, obs), obs


def fit(cfg: ExperimentConfig, spec: CalibrationSpec, *,
        method: str = "auto", allow_underdetermined: bool = False,
        verbose: bool = False, rng_seed: int = 12345, **sim_kw) -> CalibrationResult:
    """Fit the free parameters to the targets.

    method: "grid"     exhaustive over each parameter's grid
            "random"   random restarts with local refinement
            "auto"     grid when the space is small, otherwise random
    """
    if not spec.parameters:
        raise ValueError("nothing to fit: spec.parameters is empty")
    if not spec.fitted_targets:
        raise ValueError("nothing to fit to: every target has fitted=False")
    if len(spec.parameters) > len(spec.fitted_targets) and not allow_underdetermined:
        raise ValueError(
            f"{len(spec.parameters)} free parameters against "
            f"{len(spec.fitted_targets)} fitted targets: the problem is "
            f"underdetermined and any fit would be arbitrary. Add targets, remove "
            f"parameters, or pass allow_underdetermined=True if you know why.")

    import random
    rng = random.Random(rng_seed)
    t0 = time.time()
    grid_size = 1
    for p in spec.parameters:
        grid_size *= len(p.grid())
    if method == "auto":
        method = "grid" if grid_size <= spec.max_evaluations else "random"

    best_vals: dict[str, float] | None = None
    best_score = float("inf")
    best_obs: dict[str, float] = {}
    history: list[tuple[dict[str, float], float]] = []
    evals = 0

    def consider(vals: dict[str, float]) -> float:
        nonlocal best_vals, best_score, best_obs, evals
        evals += 1
        s, obs = _evaluate(cfg, spec, vals, sim_kw)
        history.append((dict(vals), s))
        if s < best_score:
            best_score, best_vals, best_obs = s, dict(vals), obs
            if verbose:
                shown = ", ".join(f"{k.split('.')[-1]}={v:g}" for k, v in vals.items())
                print(f"    [{evals:>4}] {s:.4f}   {shown}")
        return s

    truncated = False
    if method == "grid":
        import itertools
        grids = [p.grid() for p in spec.parameters]
        for combo in itertools.product(*grids):
            if evals >= spec.max_evaluations:
                truncated = True
                break
            consider({p.path: v for p, v in zip(spec.parameters, combo)})
    else:
        budget = spec.max_evaluations
        per = max(4, budget // max(1, spec.restarts))
        for _ in range(spec.restarts):
            if evals >= budget:
                break
            cur = {p.path: p.clamp(rng.uniform(p.low, p.high)) for p in spec.parameters}
            cur_s = consider(cur)
            # Local refinement: shrink the neighbourhood as it converges.
            span = {p.path: (p.high - p.low) / 4 for p in spec.parameters}
            for _ in range(per - 1):
                if evals >= budget:
                    break
                cand = {p.path: p.clamp(cur[p.path] + rng.uniform(-span[p.path], span[p.path]))
                        for p in spec.parameters}
                s = consider(cand)
                if s < cur_s:
                    cur, cur_s = cand, s
                else:
                    for k in span:
                        span[k] *= 0.85

    assert best_vals is not None
    return CalibrationResult(spec=spec, best=best_vals, objective=best_score,
                             observed=best_obs, evaluations=evals,
                             seconds=time.time() - t0, history=history,
                             method=method, truncated=truncated)


def refit_under(cfg: ExperimentConfig, spec: CalibrationSpec,
                variants: dict[str, Callable[[ExperimentConfig], ExperimentConfig]],
                **fit_kw) -> dict[str, CalibrationResult]:
    """Refit the same targets under structural variations of the model.

    This is the fair test of a conclusion. If a finding only requires the model
    to match the record, then refitting each variant to the same targets should
    preserve it. Findings that survive are properties of the mechanism; findings
    that move are properties of one particular construction.
    """
    import copy
    out: dict[str, CalibrationResult] = {}
    for name, transform in variants.items():
        out[name] = fit(transform(copy.deepcopy(cfg)), spec, **fit_kw)
    return out
