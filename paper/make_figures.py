#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Regenerate the figures in the SoftwareX article from the code and configurations.

Every figure that carries data is produced here, from a configuration file and a
figure specification. Nothing is drawn by hand and no numbers are typed in.

    python paper/make_figures.py all
    python paper/make_figures.py attribution --spec paper/figures/indigo.figspec.json
    python paper/make_figures.py calibration --config configs/example_p2p.yaml
    python paper/make_figures.py calibration --config configs/example_p2p.yaml

Which figures are configuration-generic
---------------------------------------
    architecture   invariant; describes the engine, not any experiment
    viewer         any configuration
    topologies     any two configurations
    attribution    any configuration, given a set of causes in the figspec
    recovery       any configuration
    calibration    any configuration, given daily targets in the figspec
    validation     needs observed outcomes to compare against, so it needs a
                   dataset in the shape of examples/bts_validation/

A figspec is a small JSON file naming the causes to decompose and the targets to
calibrate against. Those are properties of the study, not of the software, so
they live in data rather than in this script. See paper/figures/*.figspec.json.

Screenshot figures need a browser: pip install playwright && playwright install
chromium. Everything else needs matplotlib only.
"""
from __future__ import annotations

import argparse
import copy
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
FIGDIR = ROOT / "paper" / "figures"

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch, FancyBboxPatch

from airresilience.model import load_experiment
from airresilience.metrics import attribute, replicate

# House style, so every figure in the article matches.
NAVY, GOLD, RED, SLATE, MIST = "#1F3A4D", "#D69A2E", "#C0392B", "#6B8A99", "#C7D0D5"
plt.rcParams.update({
    "font.family": "DejaVu Sans", "font.size": 11,
    "axes.spines.top": False, "axes.spines.right": False,
    "axes.edgecolor": "#8C9BA5", "axes.labelcolor": "#33414B",
    "text.color": "#33414B", "xtick.color": "#5A6B76", "ytick.color": "#5A6B76",
    "axes.grid": True, "grid.color": "#E4E9EC", "grid.linewidth": 0.8,
    "figure.dpi": 300, "savefig.dpi": 300, "savefig.bbox": "tight",
})


def pct(ax, axis="y", top=None):
    fmt = matplotlib.ticker.FuncFormatter(lambda v, _: f"{v:.0f}%")
    (ax.yaxis if axis == "y" else ax.xaxis).set_major_formatter(fmt)
    if top is not None:
        (ax.set_ylim if axis == "y" else ax.set_xlim)(0, top)


def load_spec(path):
    spec = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    cfg = load_experiment(ROOT / spec["config"])
    return spec, cfg


def apply_ops(cfg, ops):
    """Apply a list of {path, value} edits to a copy of a configuration.

    Causes and baselines are expressed as data in the figspec rather than as
    Python lambdas, so a different study can describe its own causes without
    editing this file.
    """
    c = copy.deepcopy(cfg)
    for op in ops:
        target, attr = c, op["path"].split(".")
        for part in attr[:-1]:
            target = getattr(target, part)
        setattr(target, attr[-1], _coerce(op["value"]))
    return c


def _coerce(value):
    """JSON object keys are always strings; day-indexed maps need integers."""
    if isinstance(value, dict):
        return {(int(k) if isinstance(k, str) and k.lstrip("-").isdigit() else k):
                _coerce(v) for k, v in value.items()}
    return value


# --------------------------------------------------------------------------
# Figure 5: Shapley attribution
# --------------------------------------------------------------------------
def fig_attribution(spec, cfg, out):
    seeds = range(*spec["seeds"])
    sim_kw = spec.get("sim_kw", {})
    base = apply_ops(cfg, spec["baseline"])
    causes = {name: (lambda ops: (lambda c: apply_ops(c, ops)))(ops)
              for name, ops in spec["causes"].items()}

    a = attribute(base, causes, seeds=seeds, metric=spec.get("metric", "cancel_pct"),
                  **sim_kw)
    names = list(spec["causes"])
    values = {n: a.values[n] for n in names}
    total = a.total

    # Record the numbers so the article's text and this figure cannot drift.
    data = {"config": spec["config"], "seeds": list(seeds),
            "causes": names, "baseline": a.baseline, "total": a.total,
            "values": values, "shares": a.shares(),
            "coalitions": {"".join(str(b) for b in k): v
                           for k, v in a.coalitions.items()},
            "coalition_table": a.coalition_table()}
    (FIGDIR / "attribution.json").write_text(
        json.dumps(data, indent=1, default=str), encoding="utf-8")

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.6, 4.4))
    for a_ in (ax1, ax2):
        a_.set_axisbelow(True)

    # (a) waterfall
    order = spec.get("waterfall_order", names)
    palette = spec.get("colours", {})
    labels = ["baseline"] + order + ["observed"]
    bottom, xs = a.baseline, []
    ax1.bar(0, a.baseline, color=MIST, width=0.62)
    for i, n in enumerate(order, start=1):
        v = values[n]
        ax1.bar(i, v, bottom=bottom, color=palette.get(n, SLATE), width=0.62)
        ax1.text(i, bottom + v + 0.55, f"+{v:.1f}", ha="center", fontweight="bold",
                 color=palette.get(n, SLATE), fontsize=10)
        bottom += v
        xs.append(i)
    ax1.bar(len(order) + 1, total, color=NAVY, width=0.62)
    ax1.text(len(order) + 1, total + 0.55, f"{total:.1f}", ha="center",
             fontweight="bold", color=NAVY, fontsize=10)
    ax1.set_xticks(range(len(labels)))
    import textwrap
    ax1.set_xticklabels(["\n".join(textwrap.wrap(l, 10)) for l in labels], fontsize=9)
    ax1.set_ylabel(spec.get("ylabel", "Cancellations"))
    ax1.set_title("(a) Shapley attribution", loc="left", fontsize=12.5, color=NAVY)
    pct(ax1, top=max(30, total * 1.22))
    ax1.grid(axis="x", visible=False)

    # (b) coalitions worth reading
    picks = spec["coalitions_shown"]
    tbl = {frozenset(c for c in names if r[c]): r["outcome"]
           for r in a.coalition_table()}
    vals = [tbl[frozenset(p["causes"])] for p in picks]
    ypos = range(len(picks))
    cols = [p.get("colour", SLATE) for p in picks]
    ax2.barh(list(ypos), vals, color=cols, height=0.62)
    for y, v in zip(ypos, vals):
        ax2.text(v + max(vals) * 0.015, y, f"{v:.1f}%", va="center", fontsize=10)
    ax2.set_yticks(list(ypos))
    ax2.set_yticklabels([p["label"] for p in picks], fontsize=10)
    ax2.invert_yaxis()
    ax2.set_xlabel(spec.get("ylabel", "Cancellations"))
    ax2.set_title("(b) Causes interact: read coalitions first", loc="left",
                  fontsize=12.5, color=NAVY)
    pct(ax2, axis="x", top=max(30, max(vals) * 1.22))
    ax2.grid(axis="y", visible=False)

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    return {n: round(s, 1) for n, s in a.shares().items()} | {
        "coalitions": {p["label"]: round(v, 2) for p, v in zip(picks, vals)}}


# --------------------------------------------------------------------------
# Figure 3: calibration against published outcomes, and recovery capacity
# --------------------------------------------------------------------------
def fig_calibration_and_recovery(spec, cfg, out):
    seeds = range(*spec["seeds"])
    sim_kw = spec.get("sim_kw", {})
    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(12.6, 4.2))
    for a_ in (ax1, ax2):
        a_.set_axisbelow(True)

    obs = spec.get("published_daily")
    ho = spec.get("held_out_target")
    if obs:
        import numpy as np
        ndays = spec.get("plot_days", 7)
        runs = [_daily_metric(cfg, s, spec.get("daily_metric", "otp_pct"), sim_kw)[:ndays]
                for s in seeds]
        arr = np.array(runs, dtype=float)
        mean, sd = arr.mean(axis=0), arr.std(axis=0)
        days = list(range(1, len(mean) + 1))
        ax1.fill_between(days, mean - sd, mean + sd, color=RED, alpha=0.16, lw=0)
        ax1.plot(days, mean, "-o", color=RED, ms=5, lw=2,
                 label=f"Model, {len(runs)} schedules")
        # Published points exist only where a published figure exists. Days with
        # no observation are left empty rather than filled with a plausible one.
        ax1.plot(list(range(1, len(obs) + 1)), obs, "o", color=NAVY, ms=7,
                 label="Published, fitted")
        if len(mean) > len(obs):
            ax1.axvspan(len(obs) + 0.5, len(mean) + 0.5, color="#F2F5F6", zorder=0)
            ax1.text(len(obs) + 0.7, 93, "no published punctuality", fontsize=8.5,
                     color="#7A8892")
        if ho:
            ax1.plot([], [], "s", color="#1F6F5C", ms=7,
                     label=f"Held out: day {ho['day']} {ho['metric']} = {ho['value']}%")
        ax1.set_xticks(days)
        ax1.set_xlabel(spec.get("daily_xlabel", "Day"))
        ax1.set_ylabel("On-time performance")
        ax1.legend(frameon=False, fontsize=8.8, loc="lower left",
                   bbox_to_anchor=(0.02, 0.02))
        pct(ax1, top=100)
    ax1.set_title("(a) Calibration against published punctuality", loc="left", fontsize=12.5, color=NAVY)

    levels = spec.get("standby_levels", [0, 3, 6, 9, 12, 15, 20])
    ys = []
    for lv in levels:
        c = copy.deepcopy(cfg)
        c.policy.standby_pct = lv
        ys.append(replicate(c, seeds, "cancel_pct", **sim_kw).mean)
    ax2.plot(levels, ys, "-o", color="#1F6F5C", ms=5, lw=2)
    ax2.annotate("as flown", (levels[0], ys[0]), textcoords="offset points",
                 xytext=(12, 6), fontsize=9.5, color=RED)
    ax2.set_xlabel("Standby crews (% of establishment)")
    ax2.set_ylabel("Cancellations")
    ax2.set_title("(b) Response to recovery capacity", loc="left", fontsize=12.5, color=NAVY)
    pct(ax2, top=max(ys) * 1.15)

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)
    return {"standby_levels": levels, "cancellations": [round(y, 2) for y in ys]}


def _daily_metric(cfg, seed, metric, sim_kw):
    from airresilience.engine import Simulator
    c = copy.deepcopy(cfg)
    c.seed = seed
    res = Simulator(c, **sim_kw).run()
    return [getattr(d, metric) for d in res.days]


# --------------------------------------------------------------------------
# Figures 2 and 4: the viewer, screenshotted from the real thing
# --------------------------------------------------------------------------
def shoot(html, out, width=1400, height=1350, clip=None, scrub=None, day=None):
    """Screenshot the real viewer rendering a real trace.

    `scrub` advances playback to a fraction of the day before the shot, which
    matters for the comparison figure: at midnight the two policies are
    identical and the figure would show nothing.
    """
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": width, "height": height},
                        device_scale_factor=2)
        pg.goto(pathlib.Path(html).resolve().as_uri())
        pg.wait_for_timeout(2000)
        if day is not None:
            # Day tabs live in #days and are labelled from the trace, so match
            # by label text when given a string and by position when given an
            # index. Clicking is what a reader would do.
            pg.evaluate("""(d) => {
                const tabs = [...document.querySelectorAll('#days button')];
                const b = (typeof d === 'number')
                    ? tabs[d]
                    : tabs.find(x => x.textContent.trim() === String(d));
                if (b) b.click();
            }""", day)
            pg.wait_for_timeout(600)
        if scrub is not None:
            pg.evaluate("""(v) => {
                const s = document.querySelector('#scrub');
                s.value = v;
                s.dispatchEvent(new Event('input', {bubbles: true}));
            }""", int(scrub * 1000))
            pg.wait_for_timeout(900)
        pg.screenshot(path=str(out), clip=clip)
        b.close()


def shoot_element(html, out, selector, scrub=None, day=None,
                  width=1400, height=1360):
    """Screenshot one element of the viewer rather than the whole page."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": width, "height": height},
                        device_scale_factor=2)
        pg.goto(pathlib.Path(html).resolve().as_uri())
        pg.wait_for_timeout(2000)
        if day is not None:
            # Day tabs live in #days and are labelled from the trace, so match
            # by label text when given a string and by position when given an
            # index. Clicking is what a reader would do.
            pg.evaluate("""(d) => {
                const tabs = [...document.querySelectorAll('#days button')];
                const b = (typeof d === 'number')
                    ? tabs[d]
                    : tabs.find(x => x.textContent.trim() === String(d));
                if (b) b.click();
            }""", day)
            pg.wait_for_timeout(700)
        if scrub is not None:
            pg.evaluate("""(v) => {
                const s = document.querySelector('#scrub');
                s.value = v;
                s.dispatchEvent(new Event('input', {bubbles: true}));
            }""", int(scrub * 1000))
            pg.wait_for_timeout(900)
        pg.locator(selector).first.screenshot(path=str(out))
        b.close()


def bundle(traces, out_html):
    import subprocess
    subprocess.run([sys.executable, str(ROOT / "viewer" / "bundle.py"),
                    *[str(t) for t in traces], "-o", str(out_html)], check=True)


def run_to_trace(cfg_path, out_trace, label=None, day_labels=None, **overrides):
    """Run a configuration and write a trace, with a short label for the viewer.

    The label matters for figures: a configuration's `description` can run to
    several sentences, which the viewer will faithfully print as a heading.
    """
    from airresilience.engine import Simulator
    from airresilience.emit import emit
    cfg = load_experiment(cfg_path) if not hasattr(cfg_path, "policy") else cfg_path
    c = copy.deepcopy(cfg)
    sim_kw = {}
    for k, v in overrides.items():
        if k == "standby":
            c.policy.standby_pct = v
        elif k == "roster":
            c.policy.roster_mode = v
        elif k == "seed":
            c.seed = v
        elif k == "congestion":
            sim_kw["congestion_minutes"] = v
    emit(Simulator(c, **sim_kw).run(), label=label or c.name,
         day_labels=day_labels).write(str(out_trace))



def fig_viewer(spec, out):
    """Figure 2: the viewer itself, comparing two policies on one configuration.

    The screenshot is taken of the real viewer rendering real traces, so it
    cannot drift from what the software actually does.
    """
    import tempfile
    cfgp = ROOT / spec["config"]
    pol = spec.get("viewer_policies",
                   [{"label": "as flown, no standby", "standby": 0},
                    {"label": "12% standby", "standby": 12}])
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        paths = []
        for i, pp in enumerate(pol):
            pp = dict(pp); lab = pp.pop("label", None)
            run_to_trace(cfgp, d / f"p{i}.trace.json", label=lab,
                         day_labels=spec.get("day_labels"), **pp)
            paths.append(d / f"p{i}.trace.json")
        bundle(paths, d / "v.html")
        shoot(d / "v.html", out, width=1400, height=1360,
              scrub=spec.get("viewer_scrub", 0.78), day=spec.get("viewer_day"))


def fig_topologies(spec, out):
    """Figure 4: a second configuration under two recovery policies.

    Both runs go into one viewer page so the comparison chrome renders, exactly
    as a reader would see it after loading two traces.
    """
    import tempfile
    cfgs = spec.get("topology_configs", [
        {"config": "configs/example_p2p.yaml", "label": "no standby cover", "standby": 0},
        {"config": "configs/example_p2p.yaml", "label": "15% standby", "standby": 15}])
    with tempfile.TemporaryDirectory() as d:
        d = pathlib.Path(d)
        paths = []
        for i, s in enumerate(cfgs):
            s = dict(s)
            cpath = s.pop("config"); lab = s.pop("label", None)
            run_to_trace(ROOT / cpath, d / f"t{i}.trace.json", label=lab, **s)
            paths.append(d / f"t{i}.trace.json")
        bundle(paths, d / "t.html")
        shoot_element(d / "t.html", out, "#panes",
                      scrub=spec.get("topology_scrub", 0.95),
                      day=spec.get("topology_day"))


# --------------------------------------------------------------------------
# Figure 1: the architecture diagram, drawn rather than illustrated
# --------------------------------------------------------------------------
def fig_architecture(out):
    fig, ax = plt.subplots(figsize=(11.5, 5.6))
    ax.set_xlim(0, 100); ax.set_ylim(0, 48); ax.axis("off"); ax.grid(False)

    def box(x, y, w, h, label, sub=None, fill="#F4F7F8", edge=NAVY, bold=True):
        ax.add_patch(FancyBboxPatch((x, y), w, h, boxstyle="round,pad=0.6,rounding_size=1.2",
                                    fc=fill, ec=edge, lw=1.4))
        ax.text(x + w / 2, y + h / 2 + (1.6 if sub else 0), label, ha="center", va="center",
                fontsize=11, fontweight="bold" if bold else "normal", color=NAVY)
        if sub:
            ax.text(x + w / 2, y + h / 2 - 2.4, sub, ha="center", va="center",
                    fontsize=8.8, color="#63737E")

    def arrow(x1, y1, x2, y2, style="-|>", dashed=False, colour=NAVY):
        ax.add_patch(FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                                     mutation_scale=14, lw=1.3, color=colour,
                                     linestyle="--" if dashed else "-",
                                     connectionstyle="arc3,rad=0"))

    box(1, 30, 19, 13, "Configuration", "network, fleet, crew,\nschedule, policy", fill="#FFFFFF")
    box(1, 12, 19, 13, "Rule set", "duty caps, rest,\nrolling windows, night", fill="#FFFFFF")
    box(27, 21, 18, 13, "Roster", "legs grouped into\nduties under the cap", fill="#FDF6E8", edge=GOLD)
    box(52, 21, 19, 13, "Day loop", "position, delay,\nlegality, standby", fill="#FBEDEB", edge=RED)
    box(79, 21, 19, 13, "Trace", "every leg and event,\nparameter provenance", fill="#EEF2F4")

    arrow(20, 36, 27, 30); arrow(20, 18, 27, 25)
    arrow(45, 27.5, 52, 27.5); arrow(71, 27.5, 79, 27.5)
    arrow(61.5, 21, 61.5, 11, dashed=True, colour=SLATE)
    arrow(61.5, 11, 36, 11, style="-", dashed=True, colour=SLATE)
    arrow(36, 11, 36, 21, dashed=True, colour=SLATE)
    ax.text(48.5, 8.6, "overnight carry-over: position persists, readiness resets",
            ha="center", fontsize=9.2, color=SLATE, style="italic")
    ax.text(88.5, 17.5, "viewer renders any trace", ha="center", fontsize=9.2, color="#63737E")

    fig.tight_layout()
    fig.savefig(out)
    plt.close(fig)


# --------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("figure", choices=["all", "architecture", "viewer", "topologies",
                                       "calibration", "attribution", "validation"])
    ap.add_argument("--spec", default=str(FIGDIR / "indigo.figspec.json"))
    ap.add_argument("--config", help="override the configuration named in the figspec")
    ap.add_argument("--out", help="output PNG path")
    args = ap.parse_args()

    spec, cfg = load_spec(args.spec)
    if args.config:
        cfg = load_experiment(ROOT / args.config)

    wants = (["architecture", "viewer", "calibration", "topologies", "attribution"]
             if args.figure == "all" else [args.figure])
    for name in wants:
        out = pathlib.Path(args.out) if args.out else FIGDIR / {
            "architecture": "fig1_architecture.png", "viewer": "fig2_viewer.png",
            "calibration": "fig3_validation.png", "topologies": "fig4_topologies.png",
            "attribution": "fig5_attribution.png", "validation": "fig6_validation_bts.png",
        }[name]
        print(f"-> {name}: {out.name}")
        if name == "architecture":
            fig_architecture(out)
        elif name == "attribution":
            print("   ", fig_attribution(spec, cfg, out))
        elif name == "calibration":
            print("   ", fig_calibration_and_recovery(spec, cfg, out))
        elif name == "viewer":
            fig_viewer(spec, out)
        elif name == "topologies":
            fig_topologies(spec, out)
        elif name == "validation":
            print("    needs a dataset of observed outcomes; see adapters/validate_bts.py")
    print("done")


if __name__ == "__main__":
    raise SystemExit(main())
