# Contributing to AirResilience

Contributions are welcome. This file describes what the project needs from a
change, and the one constraint that is stricter than it looks.

## The parity suite constrains the engine

`tests/test_parity.py` checks the configurable engine against the fixed
reference implementation in `reference/hub_reference.py`, comparing 40,040
individual legs across 20 scenarios on state, cause and realised departure time.
Agreement is exact.

That suite is what makes the engine safe to change: a configuration-driven
simulator has many ways to be subtly wrong that still produce plausible totals,
and exact leg-level agreement catches them.
**Run it after any change to the engine, the rule DSL or the scheduler.** If a
change makes it fail, the change is either wrong or it is a deliberate revision
of the model, in which case say so explicitly in the pull request and explain
why the reference implementation is now the thing that is out of date.

Two details make runs reproducible across implementations and must be preserved:

- `js_round` rounds halves away from zero rather than to even, so a run is
  reproducible by an implementation in another language.
- Two mulberry32 streams, seeded `seed` and `seed + 4242`, are consumed in a
  fixed order. Adding a random draw in the middle of the sequence shifts every
  subsequent one and changes every result.

## Running everything

```
python tests/test_parity.py           # 931 checks
python tests/test_units.py            # 40 cases
python tests/test_viewer.py           # 22 checks, needs a browser
```

PyYAML is the only dependency and `pip install -e .` provides it. Continuous
integration runs the same commands on Python 3.10 to 3.13.

## What a good change looks like

- **Regulations stay data.** Duty caps, rest, rolling windows and night rules
  belong in a `RuleSet` in configuration, not in a branch in `engine.py`. A rule
  field left unset is simply not enforced, and that behaviour should hold for
  any field you add.
- **New behaviour comes with a unit case.** `tests/test_units.py` is plain
  Python and takes a new case in a few lines.
- **Trace changes are versioned.** The trace format has a strict validator in
  `trace.py`. Adding a field means updating the validator and the viewer, which
  is a renderer and must not acquire simulation logic of its own.
- **Viewer changes are testable.** `tests/test_viewer.py` asserts that rendered
  numbers match the trace. If a change alters what is displayed, that suite is
  where it should be caught.
- **Every source file carries an SPDX header.** Two lines at the top, matching
  the ones already there.

## Reporting a problem

Open an issue with the configuration file, the seed and the command line. Runs
are deterministic given a configuration and a seed, so that is usually enough to
reproduce exactly. A trace file (`--trace out.trace.json`) is even better, since
it records every leg, every event and the provenance of every parameter.

## Licence

By contributing you agree that your contributions are licensed under the MIT
Licence, as in [`LICENSE.txt`](LICENSE.txt).
