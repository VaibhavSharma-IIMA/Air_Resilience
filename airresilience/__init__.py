# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
AirResilience: configurable simulation of disruption propagation in scheduled
operations.

    from airresilience import load_experiment, Simulator, emit

    cfg = load_experiment("configs/indigo_bom.yaml")
    result = Simulator(cfg, congestion_minutes=40).run()
    print(result.summary())
    emit(result).write("run.trace.json")

The engine is configuration-driven and knows nothing about any particular
airline, network or regulation. Duty-time rules are data (`regulations`), runs
are recorded in a standard trace format (`trace`) that a generic browser viewer
renders, and analysis lives in `calibration` and `metrics`.
"""

__version__ = "1.0.0"

from .model import (
    Airport, Aircraft, CrewUnit, Duty, ExperimentConfig, FlightLeg, Route,
    dump_experiment, load_experiment,
)
from .regulations import NightRule, RollingLimit, RuleSet, load_ruleset
from .engine import (
    DIRECT_REASONS, DUTY_LIMIT_REACHED, NO_CREW_ASSIGNED, PROPAGATED_REASONS,
    RESOURCE_OUT_OF_POSITION, DayResult, LegOutcome, SeasonResult, Simulator,
    build_duties, build_schedule, run_experiment,
)
from .calibration import (
    CalibrationResult, CalibrationSpec, ParameterSpec, Target, fit, observe, refit_under,
)
from .metrics import (
    Attribution, Replication, StructuralSweep, attribute, replicate, structural_sweep,
)
from .emit import emit
from . import trace

__all__ = [
    "Airport", "Aircraft", "CrewUnit", "Duty", "ExperimentConfig", "FlightLeg", "Route",
    "load_experiment", "dump_experiment",
    "RuleSet", "RollingLimit", "NightRule", "load_ruleset",
    "Simulator", "SeasonResult", "DayResult", "LegOutcome",
    "build_schedule", "build_duties", "run_experiment",
    "DUTY_LIMIT_REACHED", "RESOURCE_OUT_OF_POSITION", "NO_CREW_ASSIGNED",
    "DIRECT_REASONS", "PROPAGATED_REASONS",
    "CalibrationSpec", "ParameterSpec", "Target", "CalibrationResult",
    "fit", "observe", "refit_under",
    "replicate", "attribute", "structural_sweep",
    "Replication", "Attribution", "StructuralSweep",
    "emit", "trace", "__version__",
]
