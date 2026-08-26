# AirResilience

[![DOI](https://zenodo.org/badge/DOI/10.5281/zenodo.22105547.svg)](https://doi.org/10.5281/zenodo.22105547)
[![tests](https://github.com/VaibhavSharma-IIMA/Air_Resilience/actions/workflows/tests.yml/badge.svg)](https://github.com/VaibhavSharma-IIMA/Air_Resilience/actions/workflows/tests.yml)

A configurable simulator for disruption propagation in **airline operations**,
with a standard trace format and a browser viewer that needs no installation.

Built for airline operations. The engine is configuration-driven throughout:
network, fleet, crew, schedule and duty-time rules are all declared in a file,
so a carrier, a regulator or a network shape is a configuration rather than a
code change.

Requires Python 3.10 or later. Runs on Linux, macOS and Windows. No compilation
step and no required dependencies.

**Citing this work.** Use the "Cite this repository" button on GitHub, or
[`CITATION.cff`](CITATION.cff) directly. The archived release is:

> Narayanaswami, S. and Sharma, V. (2026). *AirResilience: configurable
> simulation of disruption propagation in scheduled operations*, version 1.0.0.
> Zenodo. https://doi.org/10.5281/zenodo.22105547

If you use the software in published work, please cite the accompanying
SoftwareX article as well as this release.

---

## Install

    pip install -e .

The only dependency is PyYAML, which the shipped configurations need and pip
installs for you. Nothing else is required: the engine, the trace format and the
viewer use the standard library alone. Installing gives you an `airresilience`
command; without installing, `python run.py` does the same thing.

Installing provides the library and the command, not the repository. The example
configurations, the viewer and the validation datasets are files in this
checkout, so run the commands below from the repository root. The command works
on any configuration you write, wherever it lives.

## Five minute demo

    python tests/test_parity.py            # 931 checks
    python tests/test_units.py             # 40 cases
    python tests/test_viewer.py            # 22 checks, needs a browser

Both suites run in a few seconds on a current machine.

    python run.py configs/indigo_bom.yaml                  # one week, day by day
    python run.py configs/indigo_bom.yaml --sweep standby 0 4 8 12 20 --seeds 101-110
    python run.py configs/indigo_bom.yaml --standby 12 --trace b.trace.json
    python run.py configs/indigo_bom.yaml --trace a.trace.json

    python viewer/bundle.py a.trace.json b.trace.json -o demo.html
    open demo.html                         # or open viewer/viewer.html and drag files in

Then, for the analysis layer (about four minutes):

    python examples/analysis_demo.py                     # the hub study
    python examples/analysis_demo.py --spec mystudy.json  # any other study

which runs a blind calibration, a Shapley attribution and a structural sweep.

In the viewer, press **Play**. Both panes fly the same day under different
policies; the chart underneath shows cumulative cancellations against the clock.
The point to watch is that the two lines stay together through the morning and
separate in the afternoon: the damage appears hours after the shock, once delay
has had time to push crews past their limits.

---

## What it models

Each day, every leg is asked four questions in scheduled-departure order:

1. Is the aircraft at the departure airport? If not, the leg is lost, and nothing
   else about it matters.
2. When can it actually push back? Delay accumulates from the previous leg,
   weather, and hub congestion.
3. Is the crew still legal on arrival? If not, standby cover is tried.
4. Does the duty have a crew at all?

Position and readiness then carry into the next morning, limited by overnight
repositioning capacity. **That carry-over is the point.** It is what makes a week
different from seven independent days, and it is where propagation comes from:
in the demonstration case roughly three quarters of lost flights are displaced
rather than directly caused.

---

## Layout

    airresilience/
      model.py          entities and experiment configuration
      regulations.py    duty-time rules as data (the rule DSL)
      engine.py         the simulator
      calibration.py    targets, objectives, search
      metrics.py        replication, Shapley attribution, structural sweeps
      emit.py           SeasonResult -> trace
      trace.py          trace format, builder, validator
      cli.py            command line
    configs/            experiments: airline hub and point-to-point mesh
    viewer/             generic renderer + bundler
    adapters/           BTS ingest, validation, and an example foreign-simulator wrapper
    paper/              figure generation and the study spec
    tests/              parity (931), units (40), viewer (22)
    reference/          fixed hub-case implementation, kept only as a test oracle
    docs/               validation method and results
    examples/           traces, schedules, the BTS validation datasets

---

## Which lever matters

Three questions the analysis layer answers, all from the command line or a few
lines of Python.

**How much is propagation rather than the shock?** Every run reports direct
cancellations, propagated ones and the ratio between them:

    week: 518 of 2002 cancelled (25.87%), cascade x4.111
          by reason: duty_limit_reached 126, resource_out_of_position 392

**What does one lever buy?** Sweep it, holding everything else and replicating
across seeds, so a difference smaller than the spread across schedules is not
read as a result:

    python run.py configs/indigo_bom.yaml --sweep standby 0 6 12 20 --seeds 101-110

**How much does each cause contribute when several act at once?** Causes
interact, so they cannot be scored one at a time. `metrics.attribute` computes
exact Shapley values over every combination:

    python examples/analysis_demo.py attribute
    python examples/analysis_demo.py attribute --metric otp_pct

Attribution works on any metric the result exposes, and the answers differ by
metric: in the hub study the rule change dominates cancellations while weather
dominates the loss of punctuality.

None of this is tied to one case. A study is a JSON file naming a
configuration, the causes to decompose, the targets to calibrate against and the
variants to sweep; the analysis code knows nothing about any particular network.
`examples/p2p.study.json` is a second study over the point-to-point
configuration, with its own causes and its own levers:

    python examples/analysis_demo.py --spec examples/p2p.study.json
    python examples/analysis_demo.py sweep --spec examples/p2p.study.json

Write one for your own configuration and the same three analyses run on it.

## Calibration

Fitting is where most of the epistemic risk in a simulation study lives, so the
API is built to make the process visible rather than convenient.

    spec = CalibrationSpec(
        parameters=[ParameterSpec("regulation.max_duty_minutes", 640, 720, step=6),
                    ParameterSpec("congestion_minutes", 20, 60, step=10)],
        targets=[Target("cancel_pct", 28.5, tolerance=2.0),
                 Target("otp_pct.0",  49.5, tolerance=8.0),
                 Target("cancel_pct.3", 27.6, tolerance=5.0, fitted=False)],
        seeds=range(101, 107))
    print(fit(cfg, spec).report())

Target values are observations of the operation being modelled. They are inputs
to a study, not properties of the software, and the framework does not supply
them: record where each came from, because a fitted parameter is only as good as
the numbers it was fitted to. The study spec used by `examples/analysis_demo.py`
keeps them in one file with a note against each.

Three things are enforced rather than documented:

- **Under-determination is refused.** Fitting more parameters than targets
  returns an error, not a number.
- **Held-out targets** (`fitted=False`) are scored but never optimised against,
  so every fit reports its own out-of-sample error.
- **Truncated searches say so**, instead of quietly returning the best point seen.

`refit_under()` refits the same targets under structural variations, which is
what makes a robustness claim meaningful rather than decorative.

## Analysis

    replicate(cfg, seeds)              spread, confidence interval, convergence
    attribute(cfg, causes, seeds)      exact Shapley decomposition
    structural_sweep(cfg, variants)    refit each variant, compare what holds

Attribution exists because additive scoring cannot handle interacting causes,
and in coupled systems they nearly always interact: a cause that does nothing
alone may triple the damage in company. Shapley is the only decomposition that is
exact, order-independent and leaves no residual. Read `coalition_table()` before
the shares.

    python examples/analysis_demo.py

runs calibration, attribution and a structural sweep on the demonstration case.

---

## Regulations are data, not code

The engine never hard-codes a regulation. It asks a `RuleSet` whether a duty may
start and whether a leg may complete.

    regulation:
      name: my-rules
      max_duty_minutes: 780
      max_legs_per_duty: 4
      min_rest_minutes: 720
      rolling:
        - {days: 7, max_duty_minutes: 3600}
      night:
        window: [0, 360]
        duty_penalty_minutes: 60
        late_finish_after: 1380
      weekly_rest_days: 1
      roster_headroom_minutes: 120

Any field left out is simply not enforced, so you can start with one limit.

**No real regulation ships with this package.** DGCA, FAA and EASA rules run to
hundreds of pages with carrier-specific approvals, and a partial implementation
that looks authoritative is worse than none. `dgca_style_2025` is provided as a
worked example, is flagged `is_reconstruction: true`, and says so in every trace
it produces.

A rule set can also model the difference between the rules in force and the rules
a roster was *planned* under. Set `policy.roster_mode: legacy` with a
`baseline_regulation`, and the schedule is built to the old limits and flown
under the new ones. That distinction separates a schedule that is merely tight
from one that cannot be staffed at all.

---

## Historical data

Two switches matter when a schedule comes from records rather than a plan.

`schedule.records_actual` tells the engine the file describes what *happened*.
Historical data contains ferry flights, aircraft swaps and repositioning that no
passenger timetable shows, and they appear as gaps in a resource's chain.
Without this flag every gap reads as a failure and the run fills with phantom
cascades; with it, only displacement the simulation itself caused propagates.

`exogenous_cancellations` hands the engine a set of legs removed by something
outside the model: a storm, a closure, a decision taken in advance. The model
does not predict them, it propagates their consequences. That separation is what
makes the propagation mechanism testable on its own, because the trigger comes
from the record and only the cascade is modelled.

    Simulator(cfg, exogenous_cancellations={101, 102, 340}).run()

Times must be on one clock. BTS publishes local times, so `bts_ingest.py` infers
each airport's offset from the schedule's own internal consistency rather than
requiring a timezone database.

## Traces

A run emits a trace: network, resources, policy, every parameter with its
provenance, every leg with what became of it. The browser only renders. There is
one engine and no port to keep in sync.

Every parameter is labelled `sourced`, `user`, `assumed`, `calibrated` or
`derived`, and the viewer colour-codes them. A reader can see which numbers are
evidence and which are modelling choices without going to an appendix.

Load two traces to compare them side by side on a synchronised clock. Metrics are
recomputed from the legs rather than trusted from the summary, so traces from
different engines are always compared on the same definitions.

Adapting a foreign simulator means writing one adapter and nothing else; see
`adapters/indigo_adapter.py`.

---

## Verification

    python tests/test_parity.py

The engine is checked against the fixed reference implementation of the hub case
in `reference/`, which serves as a test oracle. **931 assertions across 20
scenarios and 40,040 leg comparisons**, spanning four seeds, three standby
levels, both roster modes and two rule sets. Agreement is exact, down to
individual departure times, so a change that alters unrelated behaviour is
caught immediately.

`tests/test_units.py` covers what parity cannot: 40 cases over the trace
validator, configuration checks, the rule DSL, CSV ingest, engine invariants,
calibration guardrails and the Shapley decomposition. Both suites run under
pytest or standalone.

`tests/test_viewer.py` renders real traces in headless Chromium and compares
what appears on screen against the trace that produced it: the seed, the fleet
size, the week and per-day totals, the cancellation reasons, one tab per day.
The viewer computes nothing of its own, so this is the whole of what can be
asserted about it. It needs `pip install playwright` and `playwright install
chromium`, and skips rather than fails when they are absent.

The engine suites run in continuous integration on every push, across Python
3.10 to 3.13 on Linux and on 3.12 for macOS and Windows; the viewer suite runs
once on Linux with a browser. See
[`.github/workflows/tests.yml`](.github/workflows/tests.yml).

The parity test also confirms the engine reproduces the figures reported in the
study:
131 direct and 384 knock-on cancellations, cascade multiplier 3.92, and a 25.89%
weekly cancellation rate, each averaged over the same 20 schedules.

---

## Replication and noise

Randomly generated schedules vary. In the demonstration case the standard
deviation across 20 schedules is about 5 percentage points, so `--seeds`
replicates and reports it. **Differences smaller than that spread are not
results**, and the CLI says so rather than leaving it to the reader.

---

## Status and limits

Working: configuration, rule DSL, synthetic and file-based schedules, dated
timetables, rostering, execution, standby and repositioning recovery, exogenous
disruption injection, calibration, attribution, structural sweeps, traces,
viewer, parity and unit tests, BTS ingest and validation.

Installable with `pip install -e .`, which provides an `airresilience` command.

Not modelled: crew reassignment throughput, individual rather than pooled crew,
multi-day pairings for long-haul operations, maintenance constraints and
passenger-level outcomes. The first of these is the clearest gap; see
[`docs/validation_results.md`](docs/validation_results.md) for why.

## Licence and contributing

MIT, see [`LICENSE.txt`](LICENSE.txt). Every source file carries an SPDX header.
The licence covers the software. The accompanying article's text and figures are
subject to the publishing agreement with the journal, not to this licence; see
[`paper/README.md`](paper/README.md).
Contributions are welcome; see [`CONTRIBUTING.md`](CONTRIBUTING.md) for how the
parity suite constrains changes to the engine.

## Validating against real data

`adapters/bts_ingest.py` reconstructs real aircraft rotations from US DOT BTS
On-Time Performance data, and `adapters/validate_bts.py` calibrates on a calm
week and predicts a held-out disrupted one. See `docs/validation.md`.

The crew layer is deliberately switched off for validation. No public dataset
records which crew worked which flight, so crew legality cannot be tested against
observation, only assumed; disabling it isolates aircraft displacement, which
BTS can adjudicate exactly. Re-enabling crew later is a configuration change,
not new code.

**This has been done.** Three real Southwest Airlines periods ship in
`examples/bts_validation/`, reconstructed from December 2022 and January 2023 BTS
data:

| Period | Legs | Weather injected | Model propagates | Observed carrier |
|---|---|---|---|---|
| 1-7 Dec 2022, calm | 25,707 | 14 | 0 | 45 |
| 12-18 Jan 2023, weather | 25,015 | 305 | 61 | 43 |
| 22-28 Dec 2022, meltdown | 23,538 | 1,989 | 751 | 9,296 |

On ordinary disruption the mechanism validates: 61 modelled against 43 observed,
with nothing fitted. On the meltdown it under-predicts by an order of magnitude,
and that gap is the useful part. Southwest's collapse was a crew *assignment*
failure, not a crew *capacity* failure: the scheduling system could handle a few
hundred reassignments and was asked for thousands. This framework models capacity,
so the boundary is correctly located rather than concealed by a fit.

    python adapters/validate_bts.py examples/bts_validation/* --sensitivity

Full results and interpretation: `docs/validation_results.md`.

Not yet: a constraint on crew *reassignment throughput*, which the validation
identifies as the clearest gap; individual-pilot modelling; multi-day pairings for
long-haul; maintenance; passenger flow; pre-built regulations.

One honest caveat. The demonstration case uses a **synthetic timetable** and a
**reconstructed** rule set, so its numbers illustrate a mechanism rather than
measure an airline.

The engine takes the network as data, so hub-and-spoke and point-to-point are
both just configurations. Two ship:

| Config | Shape | Schedule |
|---|---|---|
| `indigo_bom.yaml` | airline hub, 40 aircraft | generated |
| `example_p2p.yaml` | point-to-point mesh, 8 bases | CSV |

They differ in where resources end the day, which is what drives propagation:
a hub returns everything to one base each evening, while a point-to-point
network leaves resources spread across many. Standby cover therefore has to be
held in more places to do the same work. The engine does not change; only the
configuration does.

## Regenerating the figures

    python paper/make_figures.py all
    python paper/make_figures.py attribution --spec paper/figures/indigo.figspec.json
    python paper/make_figures.py calibration --config configs/example_p2p.yaml

Every figure in the article is produced by `paper/make_figures.py` from a
configuration and a study spec. No number is typed in by hand, and the
attribution figure writes `paper/figures/attribution.json` alongside the PNG so
the article's text and the chart cannot drift apart.

A figspec (`paper/figures/*.figspec.json`) declares the causes to decompose,
the baseline to decompose against, and any published targets. Those are
properties of a study rather than of the software, so they live in data. Copy
one and change the config to produce the same figures for another experiment.

Screenshot figures render the real viewer in headless Chromium, so they cannot
drift from what the software does. They need `pip install playwright` and
`playwright install chromium`; everything else needs matplotlib only.
