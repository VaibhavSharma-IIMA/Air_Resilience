# Validating against a real timetable

The distributed case study uses a generated timetable, so its figures illustrate
a mechanism rather than measure an operator. This document describes how to test
the propagation mechanism against real published data, and is deliberately clear
about what such a test can and cannot establish.

## What can be validated, and what cannot

The model propagates disruption two ways: aircraft become stranded out of
position, and crews reach their legal duty limits.

**Aircraft displacement can be validated.** US DOT BTS On-Time Performance data
records every domestic US flight with its tail number, so a real aircraft
rotation can be reconstructed exactly rather than assumed.

**Crew legality cannot.** No public dataset anywhere records which crew worked
which flight. Rosters are commercially sensitive and are not published by any
carrier or regulator.

The honest response is to disable the crew layer for validation rather than to
invent it. With an unconstrained rule set, every duty is always legal and always
staffed, so the only cancellation the model can produce is
`resource_out_of_position`. The comparison is then a genuine test of one
mechanism, not a test of assumptions dressed up as a test of a model.

That mechanism accounts for roughly three quarters of the cascade in the
distributed case study, so it is worth testing on its own.

## Getting the data

BTS blocks scripted download; fetch it by hand from

    https://www.transtats.bts.gov/DL_SelectFields.aspx?gnoyr_VQ=FGJ

Choose *Reporting Carrier On-Time Performance (1987-present)*, pick a month, and
select at least:

    FlightDate, Reporting_Airline, Tail_Number, Flight_Number_Reporting_Airline,
    Origin, Dest, CRSDepTime, DepTime, CRSArrTime, ArrTime, Cancelled,
    CancellationCode, DepDelay, ArrDelay, CRSElapsedTime

Choose a month containing a known disruption, and make sure the same month also
contains a quiet week.

## Procedure

    # one calm week, for calibration
    python adapters/bts_ingest.py raw.csv --carrier WN --origin-hub MDW \
        --start 2024-01-08 --end 2024-01-14 --out calm/

    # one disrupted week, held out
    python adapters/bts_ingest.py raw.csv --carrier WN --origin-hub MDW \
        --start 2024-01-15 --end 2024-01-21 --out storm/

    python adapters/validate_bts.py calm/ storm/ --report validation.md

Each ingest writes `schedule.csv`, `config.yaml` and `observed.json`.

## Why calibrate on the calm week

Fitting parameters on the same week you then ask the model to reproduce
demonstrates curve-fitting, not mechanism: it will match, because you made it
match. Instead, only delay parameters are fitted, only on the calm week, and only
against ordinary punctuality. Cancellations in the disrupted week are never used
in fitting, so reproducing them is a prediction.

The fit is deliberately over-determined, two free parameters against three
targets, and the script warns if a parameter settles at a search boundary.

## Reading the result

Compare the modelled rate against the **carrier-coded** observed rate rather than
the total. BTS attributes cancellations to carrier, weather, national airspace or
security. Weather and airspace cancellations are largely decisions taken in
advance, whereas a propagation model produces cascades, so the carrier-coded
subset is the closer comparison. The report prints both.

Do not expect an exact match. What is informative is whether the model lands in
the right region, and whether it puts the losses on the right days.

## Re-enabling crew later

Nothing needs building. Replace the `regulation` block in the generated
`config.yaml` with a real rule set, for example:

    regulation:
      name: faa-117-style
      max_duty_minutes: 780
      max_legs_per_duty: 5
      min_rest_minutes: 600
      rolling:
        - {days: 7, max_duty_minutes: 3600}
      weekly_rest_days: 1
      roster_headroom_minutes: 60

and set `crew.units_per_aircraft` to a plausible establishment. The crew layer
resumes immediately. Be explicit in any write-up that the crew side is assumed
rather than observed, because it is.

## Testing the pipeline without the data

A synthetic file in BTS format ships with the repository so the pipeline can be
exercised end to end:

    python adapters/bts_ingest.py examples/bts_fixture/bts_sample.csv \
        --carrier WN --origin-hub MDW --start 2024-01-08 --end 2024-01-14 --out /tmp/calm
    python adapters/bts_ingest.py examples/bts_fixture/bts_sample.csv \
        --carrier WN --origin-hub MDW --start 2024-01-15 --end 2024-01-21 --out /tmp/storm
    python adapters/validate_bts.py /tmp/calm /tmp/storm

This confirms the ingest, the dated-schedule handling and the held-out
calibration all work. **It is not a validation.** The fixture is invented, so how
well the model fits it says nothing about the real world.
