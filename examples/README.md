# Examples

    hub_asflown.trace.json      the hub case as flown: no standby, roster not replanned
    hub_standby12.trace.json    the same week with 12% standby cover rostered
    hub_replanned.trace.json    the same week with the roster rebuilt under the new limits
    p2p_asflown.trace.json      the point-to-point network, no standby
    p2p_standby15.trace.json    the same week with 15% standby cover

    analysis_demo.py            calibration, attribution and structural sweeps
    p2p.study.json              a second study spec, over the point-to-point network
    bts_validation/             three weeks of real operations data (see its README)
    bts_fixture/, point_to_point/   schedules read by the shipped configurations

## Looking at a trace without running anything

The traces above are complete records of a run. Open two side by side in the
viewer to see where two policies diverge:

    python viewer/bundle.py examples/hub_asflown.trace.json \
                            examples/hub_standby12.trace.json -o compare.html

That writes a single self-contained HTML file; open it in any browser. No
installation, no server, and the viewer never recomputes anything, so what it
shows is what the run produced.

## Regenerating them

Each trace is one command, so they can be rebuilt at any time:

    python run.py configs/hub_network.yaml --trace examples/hub_asflown.trace.json
    python run.py configs/hub_network.yaml --standby 12 --trace examples/hub_standby12.trace.json
    python run.py configs/hub_network.yaml --roster compliant --trace examples/hub_replanned.trace.json
    python run.py configs/example_p2p.yaml --trace examples/p2p_asflown.trace.json
    python run.py configs/example_p2p.yaml --standby 15 --trace examples/p2p_standby15.trace.json

Runs are deterministic, so a rebuild reproduces the same file.
