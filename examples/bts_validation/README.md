# BTS validation datasets

Three real Southwest Airlines periods, reconstructed from US DOT BTS On-Time
Performance data and shipped so the validation can be re-run without the 60 MB
source download.

| Directory | Period | Character |
|---|---|---|
| `dec_calm`   | 1-7 Dec 2022   | Normal operations, 0.23% cancelled |
| `jan_mixed`  | 12-18 Jan 2023 | Ordinary winter weather, 1.39% |
| `dec_storm`  | 22-28 Dec 2022 | The Southwest meltdown, 47.9% |

Each contains:

    config.yaml        experiment, crew constraints disabled
    schedule.csv.gz    real aircraft rotations from tail numbers
    observed.json.gz   what actually happened, per leg
    exogenous.json     weather and airspace cancellations, injected as given

The gzipped files are read as they are; nothing needs unpacking first. A
schedule is expanded once, beside its archive, the first time it is used.

Times are normalised to a single clock; BTS publishes local times and the offsets
are inferred from the schedule's own internal consistency.

To run:

    python ../../adapters/validate_bts.py dec_calm jan_mixed dec_storm --sensitivity

Results and interpretation: `docs/validation_results.md`.

Source: US DOT Bureau of Transportation Statistics, Reporting Carrier On-Time
Performance, December 2022 and January 2023, filtered to carrier WN. Public
domain.
