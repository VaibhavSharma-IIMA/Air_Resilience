# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Analysis on top of simulation runs.

Three things live here, in order of how much they are worth:

    replicate()          run a configuration many times and report the spread
    attribute()          exact Shapley decomposition over interacting causes
    structural_sweep()   refit under varied assumptions and see what survives

The last two are the reason this module exists. Simulation studies routinely
report a point estimate from one configuration, which is fine when causes act
independently and useless when they interact. Both problems are addressed here
by machinery rather than by prose.

A note on reading any of it: `replicate()` reports the standard deviation across
random schedules, and no difference smaller than that spread should be treated
as a result. The functions below carry that figure through rather than leaving
the caller to remember it.
"""

from __future__ import annotations

import copy
import itertools
import math
import statistics
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Sequence

from .engine import Simulator
from .model import ExperimentConfig

Transform = Callable[[ExperimentConfig], ExperimentConfig]


# ---------------------------------------------------------------------------
# Replication
# ---------------------------------------------------------------------------

@dataclass
class Replication:
    """Summary of one configuration run over many random schedules."""
    metric: str
    values: list[float]
    seeds: list[int]

    @property
    def mean(self) -> float:
        return statistics.mean(self.values)

    @property
    def sd(self) -> float:
        return statistics.pstdev(self.values) if len(self.values) > 1 else 0.0

    @property
    def sem(self) -> float:
        return self.sd / math.sqrt(len(self.values)) if self.values else 0.0

    def ci95(self) -> tuple[float, float]:
        """Normal-approximation interval for the mean. Adequate at n >= 20."""
        h = 1.96 * self.sem
        return (self.mean - h, self.mean + h)

    def converged(self, within: float) -> bool:
        """Whether the mean is pinned down to `within` at 95% confidence."""
        return 1.96 * self.sem <= within

    def replications_for(self, within: float) -> int:
        """How many replications would be needed to pin the mean to `within`."""
        if self.sd == 0:
            return 1
        return int(math.ceil((1.96 * self.sd / within) ** 2))

    def describe(self) -> str:
        lo, hi = self.ci95()
        return (f"{self.mean:.2f} (sd {self.sd:.2f}, 95% CI {lo:.2f}-{hi:.2f}, "
                f"n={len(self.values)})")


def replicate(cfg: ExperimentConfig, seeds: Iterable[int],
              metric: Callable[[Any], float] | str = "cancel_pct",
              **sim_kw) -> Replication:
    """Run a configuration across seeds and report the distribution of a metric."""
    seeds = list(seeds)
    getter = ((lambda r: getattr(r, metric)()) if metric in ("direct", "propagated",
                                                             "cascade_multiplier")
              else (lambda r: getattr(r, metric)) if isinstance(metric, str) else metric)
    vals = []
    for s in seeds:
        c = copy.deepcopy(cfg)
        c.seed = s
        vals.append(float(getter(Simulator(c, **sim_kw).run())))
    name = metric if isinstance(metric, str) else getattr(metric, "__name__", "metric")
    return Replication(metric=name, values=vals, seeds=seeds)


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

@dataclass
class Attribution:
    """Shapley decomposition of an outcome across interacting causes."""
    causes: list[str]
    values: dict[str, float]
    coalitions: dict[tuple[int, ...], float]
    baseline: float
    total: float

    @property
    def escalation(self) -> float:
        return self.total - self.baseline

    def shares(self) -> dict[str, float]:
        e = self.escalation or 1.0
        return {k: 100 * v / e for k, v in self.values.items()}

    def residual(self) -> float:
        """Should be zero: Shapley values sum exactly to the escalation."""
        return self.escalation - sum(self.values.values())

    def report(self) -> str:
        L = [f"Attribution over {len(self.causes)} causes "
             f"({self.baseline:.2f} -> {self.total:.2f}, escalation {self.escalation:.2f})",
             ""]
        sh = self.shares()
        for c in sorted(self.causes, key=lambda k: -abs(self.values[k])):
            L.append(f"  {c:<32}{self.values[c]:>9.2f}{sh[c]:>9.0f}%")
        L.append(f"  {'sum':<32}{sum(self.values.values()):>9.2f}{'100%':>9}")
        if abs(self.residual()) > 1e-6:
            L.append(f"  ! residual {self.residual():.2e} (should be zero)")
        L.append("")
        L.append("  Interactions matter: read the coalition table before the shares.")
        return "\n".join(L)

    def coalition_table(self) -> list[dict[str, Any]]:
        rows = []
        for key, v in sorted(self.coalitions.items(), key=lambda kv: kv[1]):
            row = {c: bool(key[i]) for i, c in enumerate(self.causes)}
            row["outcome"] = round(v, 3)
            rows.append(row)
        return rows


def attribute(cfg: ExperimentConfig, causes: dict[str, Transform],
              seeds: Iterable[int] = (1,),
              metric: Callable[[Any], float] | str = "cancel_pct",
              **sim_kw) -> Attribution:
    """Decompose an outcome across causes that interact.

    Each cause is a transform switching it *on*. Every combination is run, and
    each cause receives its average marginal contribution across all orders in
    which the causes could have arrived. This is the only attribution that is
    exact, order-independent and sums to the total with no residual.

    Additive scoring cannot be used when causes interact, and in coupled systems
    they nearly always do: a cause that does nothing alone may triple the damage
    in company. The cost is 2^n runs per seed, so keep n small.

    Example
    -------
        attribute(cfg, {
            "rule change":  lambda c: set_cap(c, 678),
            "fog":          lambda c: add_fog(c),
            "roster not replanned": lambda c: set_legacy(c),
        }, seeds=range(101, 111))
    """
    names = list(causes)
    n = len(names)
    if n > 6:
        raise ValueError(f"{n} causes needs {2**n} configurations per seed; "
                         "that is almost certainly not what you want")
    seeds = list(seeds)

    coalitions: dict[tuple[int, ...], float] = {}
    for mask in itertools.product((0, 1), repeat=n):
        c = copy.deepcopy(cfg)
        for on, name in zip(mask, names):
            if on:
                c = causes[name](c)
        coalitions[mask] = replicate(c, seeds, metric, **sim_kw).mean

    # Shapley weights: |S|! (n-|S|-1)! / n!
    weights = {k: math.factorial(k) * math.factorial(n - k - 1) / math.factorial(n)
               for k in range(n)}
    values = {name: 0.0 for name in names}
    for i, name in enumerate(names):
        for mask in itertools.product((0, 1), repeat=n):
            if mask[i]:
                continue
            with_i = list(mask)
            with_i[i] = 1
            values[name] += weights[sum(mask)] * (coalitions[tuple(with_i)] - coalitions[mask])

    return Attribution(causes=names, values=values, coalitions=coalitions,
                       baseline=coalitions[tuple([0] * n)],
                       total=coalitions[tuple([1] * n)])


# ---------------------------------------------------------------------------
# Structural robustness
# ---------------------------------------------------------------------------

@dataclass
class SweepRow:
    variant: str
    metrics: dict[str, float]
    refit: dict[str, float] = field(default_factory=dict)


@dataclass
class StructuralSweep:
    rows: list[SweepRow]
    base: str = "base"

    def range_of(self, metric: str) -> tuple[float, float]:
        vals = [r.metrics[metric] for r in self.rows if metric in r.metrics]
        return (min(vals), max(vals))

    def base_value(self, metric: str) -> float | None:
        for r in self.rows:
            if r.variant == self.base:
                return r.metrics.get(metric)
        return None

    def survives(self, metric: str, *, sign_stable: bool = True,
                 relative_spread: float | None = None) -> bool:
        """Whether a conclusion holds across every variant.

        `sign_stable`      the metric never changes sign
        `relative_spread`  the spread stays within this fraction of the base value
        """
        lo, hi = self.range_of(metric)
        if sign_stable and lo * hi < 0:
            return False
        if relative_spread is not None:
            b = self.base_value(metric)
            if b:
                return (hi - lo) / abs(b) <= relative_spread
        return True

    def report(self, metrics: Sequence[str]) -> str:
        L = [f"Structural sweep over {len(self.rows)} variants, each refitted to the "
             f"same targets", ""]
        w = max(len(r.variant) for r in self.rows) + 2
        L.append("  " + "variant".ljust(w) + "".join(f"{m:>22}" for m in metrics))
        L.append("  " + "-" * (w + 22 * len(metrics)))
        for r in self.rows:
            line = "  " + r.variant.ljust(w)
            for m in metrics:
                v = r.metrics.get(m)
                line += f"{v:>22.2f}" if v is not None else f"{'-':>22}"
            L.append(line)
        L.append("")
        for m in metrics:
            lo, hi = self.range_of(m)
            b = self.base_value(m)
            flag = "" if self.survives(m) else "   <- sign changes across variants"
            L.append(f"  {m:<28} base {b:>9.2f}   range {lo:>9.2f} to {hi:>9.2f}{flag}")
        L.append("")
        L.append("  A conclusion that only needs the model to match the record should")
        L.append("  survive refitting. What moves is a property of one construction,")
        L.append("  not of the mechanism.")
        return "\n".join(L)


def structural_sweep(cfg: ExperimentConfig, variants: dict[str, Transform],
                     seeds: Iterable[int] = (1,),
                     metrics: dict[str, Callable[[Any], float]] | None = None,
                     calibration_spec: Any = None, refit_kw: dict | None = None,
                     **sim_kw) -> StructuralSweep:
    """Recompute every headline metric under varied structural assumptions.

    If `calibration_spec` is given, each variant is refitted to the same targets
    before its metrics are taken. That is the fair test: matching the record
    should confer no advantage on any particular variant.
    """
    from .calibration import fit

    metrics = metrics or {
        "cancel_pct": lambda r: r.cancel_pct,
        "cascade_multiplier": lambda r: r.cascade_multiplier(),
        "duties_per_day": lambda r: float(r.duties_per_day),
    }
    seeds = list(seeds)
    rows: list[SweepRow] = []

    for name, transform in ({"base": lambda c: c} | variants).items():
        c = transform(copy.deepcopy(cfg))
        extras = dict(sim_kw)
        refit: dict[str, float] = {}
        if calibration_spec is not None:
            # The refit must see the same simulator arguments the metrics will be
            # measured under, or it fits one world and reports another.
            res = fit(c, calibration_spec, **(refit_kw or {}), **sim_kw)
            refit = dict(res.best)
            for path, value in res.best.items():
                if "." not in path:
                    extras[path] = value
                else:
                    obj: Any = c
                    parts = path.split(".")
                    for p in parts[:-1]:
                        obj = obj[p] if isinstance(obj, dict) else getattr(obj, p)
                    if isinstance(obj, dict):
                        obj[parts[-1]] = value
                    else:
                        setattr(obj, parts[-1], value)

        vals: dict[str, float] = {}
        for mname, fn in metrics.items():
            vals[mname] = replicate(c, seeds, fn, **extras).mean
        rows.append(SweepRow(variant=name, metrics=vals, refit=refit))

    return StructuralSweep(rows=rows)
