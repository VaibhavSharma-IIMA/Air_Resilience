# Reference implementation

`indigo_model.py` is a fixed, self-contained implementation of the hub case
described in Section 3.1 of the accompanying article. It exists **only as a test
oracle** and is not part of the framework.

`tests/test_parity.py` asserts that `airresilience.engine`, driven by
`configs/indigo_bom.yaml`, reproduces it exactly: 20 scenarios spanning four
seeds, three standby levels, both roster modes and two rule sets, compared over
40,040 individual legs on state, cause and realised departure time.

That is what makes the general engine safe to change. A configuration-driven
simulator has many ways to be subtly wrong that still produce plausible totals,
and a fixed oracle catches them.

Nothing in `airresilience/` imports this module. Delete the directory and the
framework still runs; only the parity test stops working.
