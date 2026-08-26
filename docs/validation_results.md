# Validation against real operations data

Three periods of US DOT BTS On-Time Performance data for Southwest Airlines were
run through the framework. The result is a partial validation with an
informative failure, and the failure is the more useful half.

## Design

The model has two propagation mechanisms: aircraft stranded out of position, and
crews reaching duty limits. **Crew constraints are disabled throughout**, because
no public dataset records which crew worked which flight. The test therefore
isolates aircraft displacement, which BTS can adjudicate exactly through tail
numbers.

Cancellations coded by BTS as **weather or airspace** are treated as exogenous:
they are decisions taken outside the operator's control loop, largely in advance,
so they are handed to the model as given rather than predicted. The model then
propagates their consequences. Cancellations coded **carrier** are the comparison
target, because those are what an operator's own recovery either prevents or
fails to prevent.

Nothing is fitted. Given a schedule and a set of injected cancellations, the
propagation count is fully determined by the rotation structure, so it cannot be
tuned to match the answer. That is a stronger position than a calibrated fit.

## Result

| Period | Legs | Weather injected | Model propagates | Observed carrier | Model | Observed |
|---|---|---|---|---|---|---|
| 1–7 Dec 2022 (calm) | 25,707 | 14 | 0 | 45 | 0.00% | 0.18% |
| 12–18 Jan 2023 (weather) | 25,015 | 305 | 61 | 43 | 0.24% | 0.17% |
| 22–28 Dec 2022 (meltdown) | 23,538 | 1,989 | 751 | 9,296 | 3.19% | 39.49% |

Every figure is invariant to turn time, congestion feedback and repositioning
capacity, which were each swept across their plausible ranges.

## Reading it

**On ordinary disruption the mechanism validates.** In the January weather week
the model propagates 61 cancellations where 43 carrier-coded ones occurred, on a
network of 25,015 legs. Same order, slight over-prediction, from a structural
calculation with nothing fitted. On the calm week both are near zero.

**On the December meltdown it under-predicts by an order of magnitude**: 751
against 9,296. Aircraft displacement alone accounts for roughly 8% of what
happened.

That gap is not a defect. It is a measurement of how much of the meltdown was
*not* aircraft displacement, and the public record says exactly what the
remainder was. The DOT investigation and Southwest's own executives attribute the
collapse to the crew reassignment system, SkySolver, which was overwhelmed by the
volume of reassignments and had to be abandoned for manual scheduling. Crews
became unassignable, then timed out. Southwest was fined $140 million, the
largest such penalty the DOT has issued.

So the model, run with **crew constraints deliberately switched off**, reproduces
ordinary propagation and misses a meltdown whose documented cause is precisely
the mechanism that was switched off. The residual is the right size and in the
right place.

## Can enabling crew close the gap?

The obvious next question. Re-running December with a Part 117-style rule set
active, sweeping the crew establishment:

| Crews per aircraft | Displaced | Duty limit | Unstaffed | Total |
|---|---|---|---|---|
| 4.0 | 776 | 1,288 | 0 | 8.8% |
| 2.5 | 776 | 1,288 | 0 | 8.8% |
| 2.0 | 776 | 1,288 | 0 | 8.8% |
| 1.6 | 760 | 1,125 | 2,074 | 16.8% |
| 1.3 | 703 | 945 | 4,868 | 27.7% |

Duty limits alone lift the total from 3.2% to 8.8%. Reaching the observed 39.5%
requires assuming roughly 1.3 crews per aircraft, which is far below any
plausible establishment for a US major.

**So the meltdown cannot be reproduced as a legal crew shortage**, and that is the
most precise finding here. Southwest's crews were largely present and legal; what
failed was the ability to *assign* them. SkySolver could handle on the order of
300 simultaneous reassignments and was asked for thousands, so schedulers reverted
to working by hand. Crews then timed out while waiting to be told where to go.

That is an information failure, not a capacity failure, and this framework models
capacity. The gap is a scope boundary, correctly located.

## Two meltdowns, two different mechanisms

The distinction matters for how the framework should be used.

| | IndiGo, Dec 2025 | Southwest, Dec 2022 |
|---|---|---|
| Trigger | Regulatory change to duty limits | Winter storm |
| Binding constraint | Crew capacity: not enough legal hours | Crew assignment: hours existed, could not be allocated |
| Reproduced by this model | Yes | No, and correctly not |

The framework models resource capacity and legality. It reproduces a shortage of
lawful crew hours. It does not model the operator's own control loop, so an
airline that has the crew but cannot find them is outside its scope. Stating that
boundary precisely is more useful than a fit that concealed it.

## What this supports, and what it does not

Supported:

- Aircraft displacement propagates as modelled under ordinary disruption, on real
  rotations, with no fitted parameters.
- The propagation calculation is structural and not tunable.
- Displacement alone cannot produce a meltdown. Something else has to fail.

Not supported:

- Crew legality, which was disabled and remains untested against observation.
- The magnitude of a full meltdown driven by a control-system failure. Reaching
  39.5% requires an implausible establishment, which is the model saying, in its
  own terms, that something other than capacity failed.
- Any claim that the model predicts December 2022. It does not, and the reason it
  does not is the finding.

## Why this matters for the framework's central claim

The framework's demonstration case argues that IndiGo's December 2025 disruption
was driven by crew planning rather than by the shock itself. This validation
reaches the same conclusion about a different airline, on a different continent,
from real data, by a different route: aircraft displacement is insufficient, and
the unexplained remainder coincides with a documented crew system failure.

Two independent operations, both point-to-point, both lean, both collapsing
through crew rather than aircraft. That is a stronger statement than either case
alone.

## Reproducing

    python adapters/bts_ingest.py raw.csv --carrier WN --origin-hub MDW \
        --start 2022-12-22 --end 2022-12-28 --out storm/
    python adapters/validate_bts.py storm/ --mode propagation

Data: BTS On-Time Performance, December 2022 and January 2023, all carriers,
filtered to WN. Fields required are listed in `adapters/bts_ingest.py`.

## What historical data requires of the engine

Real timetables impose four requirements that a generated schedule does not.
Each is handled by the engine; each will produce badly wrong results if it is
broken by a later change.

1. **A dated timetable is not a repeated day.** `schedule_by_day()` infers which
   it has: all legs on day zero means one pattern repeated, distinct day values
   mean a real timetable to be partitioned and rostered per day. Getting this
   wrong flies a week's flights every day.
2. **Published times are local to each airport.** BTS reports local times, so
   without offsets a westbound leg appears to land after its successor departs.
   `bts_ingest.infer_timezone_offsets()` solves the offsets from the schedule's
   own consistency, `arr - dep = block + delta`, by breadth-first search from a
   reference airport. No timezone database is needed.
3. **A historical schedule records recovery, not plan.** Ferry flights and
   aircraft swaps appear as gaps in a tail's chain, and treating a gap as a
   failure produces cascades that never happened. With
   `schedule.records_actual` set, a resource the model has not itself displaced
   is taken to be wherever the record says. It defaults to false, and the parity
   suite depends on that default.
4. **A crew-disabled run needs an external trigger.** With crew constraints
   switched off nothing can initiate a cascade, so
   `exogenous_cancellations` supplies the observed weather and airspace
   cancellations as given. Without them the comparison is vacuous.
