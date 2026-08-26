# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Regulations as data.

A duty-time regulation is expressed as a `RuleSet` — a set of declarative limits
evaluated against a crew unit's accumulated state. No regulation is hard-coded
into the engine, so defining a new one is a configuration change rather than a
code change.

This is the piece that makes the framework worth reusing. The engine asks two
questions and does not care how they are answered:

    can this crew legally start this duty?      -> can_start()
    can this crew legally complete this leg?    -> can_complete()

Shipping a partial implementation of a real regulation is worse than shipping
none, so this module deliberately does not include DGCA, FAA or EASA rule sets.
It provides the vocabulary and one worked example (`dgca_style_2025`) that is
explicitly labelled as a reconstruction, not an authority.

Example
-------
    rules = RuleSet(
        name="my-regulation",
        max_duty_minutes=780,
        min_rest_minutes=720,
        max_legs_per_duty=4,
        rolling=[RollingLimit(days=7, max_duty_minutes=3600)],
        night=NightRule(window=(0, 360), duty_penalty_minutes=60),
        weekly_rest_days=1,
    )
    rules.max_duty_for(duty_touches_night=True)   # 720
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class RollingLimit:
    """A cap on accumulated duty across a rolling window of days."""
    days: int
    max_duty_minutes: int

    def exceeded(self, accumulated_minutes: int, additional: int = 0) -> bool:
        return accumulated_minutes + additional > self.max_duty_minutes


@dataclass(frozen=True)
class NightRule:
    """Reduced limits for duties that encroach on the window of circadian low.

    `window` is (start, end) in minutes from local midnight. Two independent
    tests decide whether a duty encroaches, because regulators treat starting
    early and finishing late as distinct exposures:

        early_start_before   a duty beginning before this touches the window
        late_finish_after    a duty ending after this runs into it

    `late_finish_after` matters because "still working at 23:00" is a common
    trigger even though 23:00 lies outside the window itself.

    `None` means *derive from the window*, not *disable*. To switch a trigger
    off, give it a value it can never meet: `early_start_before=0` (no duty
    begins before minute zero) or `late_finish_after=None` combined with a
    window that cannot be reached.
    """
    window: tuple[int, int] = (0, 360)
    duty_penalty_minutes: int = 0
    max_landings: int | None = None
    early_start_before: int | None = None
    late_finish_after: int | None = 1380

    def touches(self, duty_start: int, duty_end: int) -> bool:
        early = self.early_start_before if self.early_start_before is not None else self.window[1]
        if duty_start < early:
            return True
        return self.late_finish_after is not None and duty_end > self.late_finish_after

    def disabled(self) -> "NightRule":
        """A copy that never triggers, useful when varying assumptions."""
        return NightRule(window=(0, 0), duty_penalty_minutes=self.duty_penalty_minutes,
                         max_landings=self.max_landings,
                         early_start_before=0, late_finish_after=None)


@dataclass
class RuleSet:
    """A duty-time regulation expressed declaratively.

    Every field is optional except the name. A rule that is `None` is simply not
    enforced, so a researcher can begin with one limit and add others without the
    engine changing.
    """
    name: str
    description: str = ""
    authority: str = ""
    is_reconstruction: bool = False      # True when not transcribed from the source text

    max_duty_minutes: int | None = None
    max_legs_per_duty: int | None = None
    min_rest_minutes: int | None = None
    report_before_minutes: int = 0
    debrief_after_minutes: int = 0

    rolling: list[RollingLimit] = field(default_factory=list)
    night: NightRule | None = None
    weekly_rest_days: int = 0            # required days off in any seven

    # How far inside the legal cap rosters are planned. A planning policy rather
    # than a regulation, but it belongs with the limits it references.
    roster_headroom_minutes: int = 0

    # ---- queries the engine asks -----------------------------------------
    def max_duty_for(self, duty_touches_night: bool = False) -> int | None:
        if self.max_duty_minutes is None:
            return None
        if duty_touches_night and self.night:
            return self.max_duty_minutes - self.night.duty_penalty_minutes
        return self.max_duty_minutes

    def roster_cap(self, duty_touches_night: bool = False) -> int | None:
        cap = self.max_duty_for(duty_touches_night)
        return None if cap is None else cap - self.roster_headroom_minutes

    def rest_satisfied(self, since_last_duty_end: int) -> bool:
        return self.min_rest_minutes is None or since_last_duty_end >= self.min_rest_minutes

    def rolling_ok(self, accumulated: dict[int, int], additional: int = 0) -> bool:
        """`accumulated` maps window length in days to minutes already worked."""
        return all(not r.exceeded(accumulated.get(r.days, 0), additional) for r in self.rolling)

    def max_consecutive_days(self, week_days: int = 7) -> int:
        return week_days - self.weekly_rest_days

    def duty_touches_night(self, duty_start: int, duty_end: int) -> bool:
        return bool(self.night) and self.night.touches(duty_start, duty_end)

    # ---- serialisation ----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["rolling"] = [asdict(r) for r in self.rolling]
        d["night"] = asdict(self.night) if self.night else None
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RuleSet":
        d = dict(d)
        d["rolling"] = [RollingLimit(**r) for r in d.get("rolling", []) or []]
        n = d.get("night")
        if n:
            n = dict(n)
            n["window"] = tuple(n.get("window", (0, 360)))
            d["night"] = NightRule(**n)
        else:
            d["night"] = None
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})

    def describe(self) -> str:
        """A one-line summary of the rules in force.

        Deliberately ASCII: this string is printed to the console, and Windows
        terminals default to a code page that cannot encode typographic
        symbols, which turns a summary line into a crash.
        """
        bits = [f"{self.name}"]
        if self.max_duty_minutes:
            bits.append(f"duty <= {self.max_duty_minutes/60:g} h")
        if self.max_legs_per_duty:
            bits.append(f"<= {self.max_legs_per_duty} legs")
        if self.min_rest_minutes:
            bits.append(f"rest >= {self.min_rest_minutes/60:g} h")
        for r in self.rolling:
            bits.append(f"<= {r.max_duty_minutes/60:g} h per {r.days} d")
        if self.night and self.night.duty_penalty_minutes:
            bits.append(f"night -{self.night.duty_penalty_minutes} min")
        if self.weekly_rest_days:
            bits.append(f"{self.weekly_rest_days} day(s) off in 7")
        return "; ".join(bits)


# ---------------------------------------------------------------------------
# Worked example
# ---------------------------------------------------------------------------

def dgca_style_2025(*, phase: str = "pre") -> RuleSet:
    """A reconstruction in the style of the 2025 Indian FDTL revision.

    NOT a transcription of the DGCA circular. It reproduces the structure of the
    change — a reduced duty cap, a harder night penalty and a second mandatory
    day off — at the level of detail the case study needed. It is provided as a
    template for writing your own rule set, and is flagged
    `is_reconstruction=True` so that any trace it produces says so.
    """
    if phase not in ("pre", "post"):
        raise ValueError("phase must be 'pre' or 'post'")
    post = phase == "post"
    return RuleSet(
        name=f"dgca-style-2025-{phase}",
        description="Reconstruction of the pre/post 2025 Indian FDTL revision",
        authority="reconstruction, not transcribed from the circular",
        is_reconstruction=True,
        max_duty_minutes=780,
        max_legs_per_duty=4,
        min_rest_minutes=720,
        report_before_minutes=60,
        debrief_after_minutes=30,
        rolling=[RollingLimit(days=7, max_duty_minutes=3600)],
        night=NightRule(window=(0, 360), duty_penalty_minutes=60),
        weekly_rest_days=2 if post else 1,
        roster_headroom_minutes=120,
    )


BUILTIN = {"dgca_style_2025_pre": lambda: dgca_style_2025(phase="pre"),
           "dgca_style_2025_post": lambda: dgca_style_2025(phase="post")}


def load_ruleset(spec: "str | dict | RuleSet") -> RuleSet:
    """Accept a built-in name, a plain dict from config, or a RuleSet."""
    if isinstance(spec, RuleSet):
        return spec
    if isinstance(spec, str):
        if spec not in BUILTIN:
            raise KeyError(f"unknown built-in rule set {spec!r}; available: {sorted(BUILTIN)}")
        return BUILTIN[spec]()
    return RuleSet.from_dict(spec)
