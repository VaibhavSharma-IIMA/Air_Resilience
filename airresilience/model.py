# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Core entities and experiment configuration.

Everything the engine needs is described here as data. Nothing in this module
knows about any carrier, hub, or any particular study: an experiment is a network,
a fleet, a crew pool, a rule set, a schedule and a policy, all loadable from a
file.

Configuration may be YAML or JSON. YAML is used when PyYAML is installed and is
otherwise unnecessary, which keeps the package installable from the standard
library alone.
"""

from __future__ import annotations

import json
import math
import pathlib
from dataclasses import dataclass, field
from typing import Any, Iterable

from .regulations import RuleSet, load_ruleset

try:                                    # optional
    import yaml                         # type: ignore
    _HAVE_YAML = True
except Exception:                       # pragma: no cover
    _HAVE_YAML = False


# ---------------------------------------------------------------------------
# Entities
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Airport:
    code: str
    name: str = ""
    lat: float | None = None
    lon: float | None = None

    @property
    def has_position(self) -> bool:
        return self.lat is not None and self.lon is not None


@dataclass(frozen=True)
class Route:
    """A directed or undirected connection with a scheduled block time."""
    origin: str
    destination: str
    block_minutes: int
    weight: float = 1.0                 # relative frequency for synthetic generation

    def reversed(self) -> "Route":
        return Route(self.destination, self.origin, self.block_minutes, self.weight)


@dataclass
class Aircraft:
    id: str
    label: str = ""
    type: str = ""
    base: str = ""
    location: str = ""                  # mutable during a run
    ready_at: int = 0
    last_arrival: int = 0

    def __post_init__(self) -> None:
        self.label = self.label or self.id


@dataclass
class CrewUnit:
    """A unit of crewing capacity, not necessarily one person.

    Airlines roster cockpit crews as pairs, and duty limits apply to the duty
    rather than the individual, so the natural unit for a disruption model is the
    crew unit. `size` records how many licensed individuals it stands for, which
    is what converts model results into headcount.
    """
    id: str
    base: str = ""
    size: int = 2
    qualifications: tuple[str, ...] = ()
    duty_by_day: dict[int, int] = field(default_factory=dict)
    last_duty_end: int = -10 ** 9
    consecutive_days: int = 0

    def rolling_minutes(self, day: int, window_days: int) -> int:
        return sum(self.duty_by_day.get(d, 0) for d in range(max(0, day - window_days + 1), day))


@dataclass
class FlightLeg:
    id: int
    aircraft: str
    origin: str
    destination: str
    scheduled_departure: int            # minutes from local midnight
    block_minutes: int
    day: int = 0
    sequence: int = 0                   # position within the aircraft's day

    @property
    def scheduled_arrival(self) -> int:
        return self.scheduled_departure + self.block_minutes


@dataclass
class Duty:
    """A contiguous block of flying assigned to one crew unit."""
    id: int
    aircraft: str
    leg_ids: list[int]
    start: int
    end: int = 0
    touches_night: bool = False

    @property
    def minutes(self) -> int:
        return self.end - self.start


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class NetworkConfig:
    hub: str
    airports: list[Airport]
    routes: list[Route] = field(default_factory=list)

    def airport(self, code: str) -> Airport:
        for a in self.airports:
            if a.code == code:
                return a
        raise KeyError(f"unknown airport {code!r}")

    def validate(self) -> None:
        codes = {a.code for a in self.airports}
        if self.hub not in codes:
            raise ValueError(f"hub {self.hub!r} is not in the airport list")
        for r in self.routes:
            if r.origin not in codes or r.destination not in codes:
                raise ValueError(f"route {r.origin}-{r.destination} refers to an unknown airport")
            if r.block_minutes <= 0:
                raise ValueError(f"route {r.origin}-{r.destination} has non-positive block time")


@dataclass
class FleetConfig:
    count: int
    type: str = "generic"
    base: str = ""
    turn_minutes: dict[str, int] = field(default_factory=dict)   # {"hub": 40, "outstation": 35}
    slack_minutes: dict[str, int] = field(default_factory=dict)
    overnight_ground_minutes: int = 300
    repositioning_per_night: int = 0

    def turn(self, at_hub: bool) -> int:
        return self.turn_minutes.get("hub" if at_hub else "outstation", 40)

    def slack(self, at_hub: bool) -> int:
        return self.slack_minutes.get("hub" if at_hub else "outstation", 0)


@dataclass
class CrewConfig:
    units: int | None = None            # absolute number of crew units
    units_per_aircraft: float | None = None
    unit_size: int = 2                  # licensed individuals per unit
    base: str = ""
    standby_pct: float = 0.0
    callout_minutes: int = 75

    def resolve_units(self, fleet_count: int) -> int:
        if self.units is not None:
            return int(self.units)
        if self.units_per_aircraft is not None:
            return int(math.floor(self.units_per_aircraft * fleet_count + 0.5))
        raise ValueError("crew config needs either 'units' or 'units_per_aircraft'")


@dataclass
class ScheduleConfig:
    """Either a path to a schedule file, or parameters to generate one."""
    source: str = "generate"            # "generate" | "file"
    path: str = ""
    # Set when the file records what *happened* rather than what was planned.
    # Historical data reflects the operator's recovery actions: ferry flights,
    # aircraft swaps and repositioning that no passenger schedule shows. Those
    # appear as gaps in a resource's chain and are not failures. With this set,
    # a resource the simulation has not itself displaced is taken to be wherever
    # the record says, so only model-caused displacement propagates. Leave it
    # false for a planned timetable, where a gap really is an error.
    records_actual: bool = False
    # True when the file records what an operator *did* rather than what it
    # published. Observed timetables contain resource moves the schedule does not
    # show, such as ferry flights, maintenance positioning and mid-day tail
    # swaps, which appear as gaps in a rotation. Treating those gaps as failures
    # invents a cascade that never happened. With this set, a gap is accepted for
    # any resource the simulation has not itself displaced, so only modelled
    # disruption propagates.
    is_actual: bool = False
    rotations_early: int = 4            # out-and-backs for early-starting aircraft
    rotations_late: int = 3
    early_cutoff: int = 390             # a start before this counts as early
    day_start: int = 300
    stagger_minutes: int = 150
    stagger_jitter: int = 20


@dataclass
class PolicyConfig:
    """The levers an experiment varies."""
    roster_mode: str = "compliant"      # "compliant" | "legacy"
    standby_pct: float = 0.0
    repositioning_per_night: int = 0
    notes: str = ""


@dataclass
class ExperimentConfig:
    name: str
    network: NetworkConfig
    fleet: FleetConfig
    crew: CrewConfig
    schedule: ScheduleConfig
    regulation: RuleSet
    baseline_regulation: RuleSet | None = None   # rules the roster was built under
    policy: PolicyConfig = field(default_factory=PolicyConfig)
    days: int = 7
    seed: int = 1
    conditions: dict[int, list[str]] = field(default_factory=dict)   # day -> ["fog", ...]
    parameters: list[dict[str, Any]] = field(default_factory=list)   # provenance declarations
    day_labels: list[str] = field(default_factory=list)  # e.g. ["2 Dec", ...]; days are numbered if unset
    description: str = ""

    def validate(self) -> "ExperimentConfig":
        self.network.validate()
        if self.fleet.count <= 0:
            raise ValueError("fleet.count must be positive")
        if self.days <= 0:
            raise ValueError("days must be positive")
        if not 0 <= self.policy.standby_pct <= 100:
            raise ValueError("policy.standby_pct must be a percentage")
        self.crew.resolve_units(self.fleet.count)
        if self.day_labels and len(self.day_labels) != self.days:
            raise ValueError(
                f"day_labels has {len(self.day_labels)} entries but days is {self.days}")
        if self.schedule.source == "file" and not self.schedule.path:
            raise ValueError("schedule.source is 'file' but no path was given")
        return self


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def _read_structured(path: str | pathlib.Path) -> dict:
    p = pathlib.Path(path)
    text = p.read_text(encoding="utf-8")
    if p.suffix.lower() in (".yaml", ".yml"):
        if not _HAVE_YAML:
            raise RuntimeError(
                f"{p.name} is YAML but PyYAML is not installed. PyYAML is a "
                "declared dependency, so this usually means the package was not "
                "installed: run 'pip install -e .' from the repository root. "
                "JSON configurations are read without it.")
        return yaml.safe_load(text)
    return json.loads(text)


REQUIRED_SECTIONS = ("network", "fleet", "schedule")


def load_experiment(path: str | pathlib.Path) -> ExperimentConfig:
    """Load and validate an experiment from YAML or JSON."""
    p = pathlib.Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"no configuration at {p}. Configurations live in the repository's "
            f"configs/ directory, for example configs/hub_network.yaml.")
    d = _read_structured(p)
    if not isinstance(d, dict):
        raise ValueError(f"{p.name} does not contain a configuration mapping")
    missing = [s for s in REQUIRED_SECTIONS if s not in d]
    if missing:
        raise ValueError(
            f"{p.name} is missing the required section(s): {', '.join(missing)}. "
            f"A configuration needs at least network, fleet and schedule; see "
            f"configs/example_p2p.yaml for a minimal one.")
    base = p.parent

    net = d["network"]
    network = NetworkConfig(
        hub=net["hub"],
        airports=[Airport(**a) for a in net["airports"]],
        routes=[Route(**r) for r in net.get("routes", [])],
    )

    fleet = FleetConfig(**d.get("fleet", {"count": 1}))
    crew = CrewConfig(**d.get("crew", {}))
    sched = ScheduleConfig(**d.get("schedule", {}))
    if sched.source == "file" and sched.path and not pathlib.Path(sched.path).is_absolute():
        sched.path = str(base / sched.path)

    reg = load_ruleset(d["regulation"])
    baseline = load_ruleset(d["baseline_regulation"]) if d.get("baseline_regulation") else None
    policy = PolicyConfig(**d.get("policy", {}))

    conditions = {int(k): list(v) for k, v in (d.get("conditions") or {}).items()}

    cfg = ExperimentConfig(
        name=d.get("name", pathlib.Path(path).stem),
        network=network, fleet=fleet, crew=crew, schedule=sched,
        regulation=reg, baseline_regulation=baseline, policy=policy,
        days=int(d.get("days", 7)), seed=int(d.get("seed", 1)),
        conditions=conditions, parameters=list(d.get("parameters", [])),
        day_labels=list(d.get("day_labels", [])),
        description=d.get("description", ""),
    )
    return cfg.validate()


def dump_experiment(cfg: ExperimentConfig, path: str | pathlib.Path) -> pathlib.Path:
    """Write a config back out, so a run can archive exactly what produced it."""
    d = {
        "name": cfg.name, "description": cfg.description,
        "days": cfg.days, "seed": cfg.seed,
        "network": {"hub": cfg.network.hub,
                    "airports": [vars(a) for a in cfg.network.airports],
                    "routes": [vars(r) for r in cfg.network.routes]},
        "fleet": vars(cfg.fleet), "crew": vars(cfg.crew), "schedule": vars(cfg.schedule),
        "regulation": cfg.regulation.to_dict(),
        "baseline_regulation": cfg.baseline_regulation.to_dict() if cfg.baseline_regulation else None,
        "policy": vars(cfg.policy),
        "conditions": {str(k): v for k, v in cfg.conditions.items()},
        "parameters": cfg.parameters,
    }
    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    if p.suffix.lower() in (".yaml", ".yml") and _HAVE_YAML:
        p.write_text(yaml.safe_dump(d, sort_keys=False), encoding="utf-8")
    else:
        p.write_text(json.dumps(d, indent=2), encoding="utf-8")
    return p
