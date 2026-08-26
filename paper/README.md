# Figures

The figures in the accompanying article are generated from the engine, not drawn
by hand. This directory contains the script that generates them and the study
they are generated from.

    make_figures.py             regenerates every figure
    figures/*.png               the figures themselves
    figures/indigo.figspec.json the study: causes, calibration targets, variants
    figures/attribution.json    the numbers behind the attribution figure

## Regenerating

    python paper/make_figures.py all
    python paper/make_figures.py attribution
    python paper/make_figures.py calibration --config configs/example_p2p.yaml

Runs are deterministic, so the same configuration and seeds redraw the same
chart byte for byte. The attribution figure writes its own numbers to
`figures/attribution.json` alongside the PNG, which is what keeps the values
quoted in the article and the values drawn in the chart from coming apart.

The two screenshot figures drive the real viewer in headless Chromium rather
than mocking it up, so they show what the software does and nothing else. They
need `pip install playwright` followed by `playwright install chromium`. The
charts need matplotlib alone.

## The study spec

`figures/indigo.figspec.json` describes a study rather than the software: which
causes to decompose, which observations to calibrate against, which structural
assumptions to vary. `examples/analysis_demo.py` reads the same file, so the
analysis in the article and the figures illustrating it run from one description.

Copy it, point `config` at your own experiment, and both will run on your case.
`examples/p2p.study.json` is a worked second study over a different network.

Calibration targets describe the operation being modelled. In the hub study they
are estimates for a representative operation rather than measurements of any
carrier.

## Licensing

`LICENSE.txt` covers the software, including the script here. It does not cover
the article's text or the figures, which fall under the publishing agreement
with the journal. The generated manuscript files are kept out of the repository
for that reason, and to keep large binaries out of the history.
