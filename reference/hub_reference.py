# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
hub demonstration case: multi-day operations model.

A discrete-event simulation of one week at a single hub. Aircraft fly rotations,
crews carry rolling duty and rest limits across days, and aircraft begin each
morning wherever the previous evening left them.

WHAT THIS MODEL IS NOT
----------------------
It is not a measurement of the demonstration carrier. The timetable is synthetic, generated from a
seeded random draw, because the demonstration carrier's sector-level schedule is not public. The
duty and rest rules are a reconstruction of how flight time limitations generally
bind, not a reading of any published circular. Turnarounds, overnight behaviour and the
congestion feedback are modelling choices. Two parameters are fitted to four
published observations; every other number is an output of those choices.

Parameters are tagged in the code as:
    [SOURCED]      published figure
    [FITTED]       tuned against the published record
    [RECONSTRUCT]  our reading of how the rules work
    [INVENTED]     chosen because it produced plausible behaviour
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Determinism helpers
#
# The reference implementation of this model was written in JavaScript, and the
# HTML exhibits still run it so the demonstration is live. This module is a port,
# and it is verified leg-for-leg against that reference (see verify_against_js.py).
# Two JS behaviours have to be reproduced exactly or the streams diverge:
#   1. mulberry32 operates on 32-bit integers with wraparound.
#   2. Math.round rounds halves UP (-2.5 -> -2), whereas Python's round() rounds
#      halves to even. js_round below restores the JS behaviour.
# ---------------------------------------------------------------------------

_U32 = 0xFFFFFFFF


def _imul(a: int, b: int) -> int:
    """32-bit signed integer multiply, matching JavaScript's Math.imul."""
    r = (a * b) & _U32
    return r - 0x100000000 if r >= 0x80000000 else r


def mulberry32(seed: int):
    """Seeded PRNG. Reproduces the JavaScript reference stream exactly."""
    state = seed & _U32

    def rand() -> float:
        nonlocal state
        state = (state + 0x6D2B79F5) & _U32
        a = state
        # JS: t = (t + Math.imul(t ^ (t>>>7), 61|t)) ^ t
        t = _imul(a ^ (a >> 15), 1 | a) & _U32
        t = (((t + _imul(t ^ (t >> 7), 61 | t)) & _U32) ^ t) & _U32
        return ((t ^ (t >> 14)) & _U32) / 4294967296.0

    return rand


def js_round(x: float) -> int:
    """JavaScript Math.round: halves go up, not to even."""
    return math.floor(x + 0.5)


# ---------------------------------------------------------------------------
# Structural constants
# ---------------------------------------------------------------------------

# code, bearing from north, block minutes, schedule weight   [INVENTED]
SPOKES = [
    ("N01", 340, 70, 3), ("N02", 358, 100, 1), ("N03", 22, 125, 2), ("E01", 72, 160, 1),
    ("C01", 112, 85, 3), ("S01", 142, 105, 2), ("S02", 168, 100, 3), ("W01", 196, 60, 2),
]

HUB = "HUB"
N_TAILS = 40            # [INVENTED] size of the modelled sub-fleet
DUTY_PRE = 60           # [RECONSTRUCT] report time before first departure
DUTY_POST = 30          # [RECONSTRUCT] debrief after last arrival
BASE_CAP = 780          # [RECONSTRUCT] 13 h legal duty cap before the change
CALLOUT = 75            # [INVENTED] standby callout to airborne
WEEKLY_DUTY_CAP = 3600  # [RECONSTRUCT] 60 h duty in any rolling 7 days
DAY_START = 300         # 05:00, in minutes
DAY = 1440

BASE_CREWS = 3.30       # [INVENTED] baseline crews per aircraft, see StructuralConfig


@dataclass
class StructuralConfig:
    """The invented and reconstructed parameters, exposed so they can be swept.

    Section 9 of the report varies these one at a time to show which conclusions
    depend on them. Defaults reproduce the base case used throughout the report.
    """
    min_rest: int = 720           # [RECONSTRUCT] 12 h between duties
    overnight_min: int = 300      # [INVENTED] ground time before flying again
    roster_headroom: int = 120    # [INVENTED] how far inside the legal cap rosters are built
    days_off: int | None = None   # [RECONSTRUCT] None = 1 before the change, 2 after
    congestion: float = 0.0       # [FITTED] minutes of hub delay at full disruption


@dataclass
class Profile:
    """An operating profile. Establishment is expressed as crews, never as 'reserve'."""
    name: str
    crews_per_ac: float = BASE_CREWS
    pairs: tuple[int, int] = (4, 3)   # out-and-backs for early / later tails
    turn_spoke: int = 35
    turn_hub: int = 40
    slack_spoke: int = 12
    slack_hub: int = 16
    max_legs: int = 4


# Only the 7.6 : 9.1 ratio is [SOURCED] (parliamentary answer). The absolute crew level is
# [INVENTED], set so the pre-change week runs clean, which it did.
PROFILES = {
    "lean": Profile("Demonstration carrier (lean)", BASE_CREWS),
    "ratio":  Profile("Peer crew ratio", BASE_CREWS * 9.1 / 7.6),
    "peer":   Profile("Peer carrier", BASE_CREWS * 9.1 / 7.6, (3, 3), 50, 55, 22, 26),
}

# Calibrated base case. Fitted jointly to the hub cancellation rate (28.5%)
# and published on-time performance on 1, 2 and 3 December (49.5 / 35.0 / 19.7).
FITTED_FDTL_HOURS = 1.7      # [FITTED]
FITTED_CONGESTION = 40       # [FITTED]
CALIBRATED_CREWS_PER_AC = 2.925
DEFAULT_FOG_DAYS = (0, 1, 2)


# ---------------------------------------------------------------------------
# Schedule construction
# ---------------------------------------------------------------------------

@dataclass
class Leg:
    id: int
    tail: int
    frm: str
    to: str
    block: int
    sched_dep: int
    spoke: str = ""
    state: str | None = None
    reason: str | None = None
    dep: int | None = None
    arr: int | None = None
    noise: int = 0


@dataclass
class Duty:
    id: int
    tail: int
    legs: list[int]
    start: int
    end: int = 0
    night: bool = False
    mins: int = 0


def _pick_spoke(rand):
    total = sum(s[3] for s in SPOKES)
    x = rand() * total
    for s in SPOKES:
        x -= s[3]
        if x <= 0:
            return s
    return SPOKES[0]


def build_template(seed: int, prof: Profile):
    """One day's published schedule, repeated every day of the season. [INVENTED]"""
    rand = mulberry32(seed)
    tails: list[list[int]] = []
    legs: list[Leg] = []

    for t in range(N_TAILS):
        start = DAY_START + js_round(t * (150 / N_TAILS)) + js_round(rand() * 20)
        n_pairs = prof.pairs[0] if start < 390 else prof.pairs[1]
        my_legs: list[int] = []
        clock = start
        for _ in range(n_pairs):
            sp = _pick_spoke(rand)
            legs.append(Leg(len(legs), t, HUB, sp[0], sp[2], clock, sp[0]))
            my_legs.append(len(legs) - 1)
            clock += sp[2] + prof.turn_spoke + prof.slack_spoke
            legs.append(Leg(len(legs), t, sp[0], HUB, sp[2], clock, sp[0]))
            my_legs.append(len(legs) - 1)
            clock += sp[2] + prof.turn_hub + prof.slack_hub
        tails.append(my_legs)
    return tails, legs


def roster_duties(tails, legs, prof: Profile, legal_cap: int, cfg: StructuralConfig):
    """Build duty lines from the timetable under a given legal cap.

    A tighter cap forces SHORTER duties, so identical flying needs MORE of them.
    This is the structural half of the shock and was knowable a year in advance.
    """
    roster_cap = legal_cap - cfg.roster_headroom
    duties: list[Duty] = []
    for tail_id, my_legs in enumerate(tails):
        cur: Duty | None = None
        for li in my_legs:
            L = legs[li]
            would_end = L.sched_dep + L.block + DUTY_POST
            if cur is not None and len(cur.legs) < prof.max_legs and (would_end - cur.start) <= roster_cap:
                cur.legs.append(li)
            else:
                cur = Duty(len(duties), tail_id, [li], L.sched_dep - DUTY_PRE)
                duties.append(cur)
    for d in duties:
        last = legs[d.legs[-1]]
        d.end = last.sched_dep + last.block + DUTY_POST
        d.night = d.start < 360 or d.end > 1380   # touches the window of circadian low
        d.mins = d.end - d.start
    return duties


# ---------------------------------------------------------------------------
# The simulation
# ---------------------------------------------------------------------------

@dataclass
class Crew:
    id: int
    duty_min: dict = field(default_factory=dict)
    last_end: int = -9999
    consec: int = 0
    roll: int = 0


def run_season(seed: int, prof: Profile, *, days: int = 7,
               fdtl_hours: float = FITTED_FDTL_HOURS,
               fog_days=DEFAULT_FOG_DAYS, repo_cap: int = 6,
               roster_mode: str = "legacy", standby_pct: float = 0.0,
               cfg: StructuralConfig | None = None,
               record_day: int | None = None,
               record_all: bool = False) -> dict[str, Any]:
    """Simulate one week.

    roster_mode : 'legacy'    the roster published under the OLD limits and flown anyway
                  'replanned' rebuilt to comply, which needs materially more duty lines
    standby_pct : crews rostered as standby, as a share of establishment
    """
    cfg = cfg or StructuralConfig(congestion=FITTED_CONGESTION)
    rand = mulberry32(seed + 4242)
    tails, template_legs = build_template(seed, prof)

    legal_cap = BASE_CAP - fdtl_hours * 60
    duties = roster_duties(tails, template_legs, prof,
                           legal_cap if roster_mode == "replanned" else BASE_CAP, cfg)

    required_off = cfg.days_off if cfg.days_off is not None else (2 if fdtl_hours > 0 else 1)
    max_consec = 7 - required_off

    n_crew = js_round(prof.crews_per_ac * N_TAILS)
    crews = [Crew(i) for i in range(n_crew)]

    ac_loc = {t: HUB for t in range(N_TAILS)}
    ac_ready = {t: DAY_START - 60 for t in range(N_TAILS)}
    last_arr = {t: 0 for t in range(N_TAILS)}

    duty_of = {}
    for du in duties:
        for li in du.legs:
            duty_of[li] = du
    duties_by_start = sorted(duties, key=lambda d: d.start)   # stable, as in JS

    out = []
    rec = None
    all_days: list[dict] = []

    for d in range(days):
        t0 = d * DAY
        fog = d in fog_days

        # --- who is legal to work today ---
        pool = [c for c in crews if c.consec < max_consec]
        for c in pool:
            c.roll = sum(c.duty_min.get(k, 0) for k in range(max(0, d - 6), d))

        # --- assign crews to duties, least-loaded first ---
        assigned: dict[int, Crew] = {}
        busy: set[int] = set()
        unstaffed = 0
        for du in duties_by_start:
            cand = [c for c in pool
                    if c.id not in busy
                    and (t0 + du.start) - c.last_end >= cfg.min_rest
                    and c.roll + du.mins <= WEEKLY_DUTY_CAP]
            if not cand:
                unstaffed += 1
                continue
            cand.sort(key=lambda c: c.roll)   # stable
            chosen = cand[0]
            assigned[du.id] = chosen
            busy.add(chosen.id)

        # A crew not flying today is usually on rostered rest, not on call.
        # Standby is a separate roster line the airline must choose to pay for.
        callable_crews = [c for c in pool if c.id not in busy and c.roll + 400 <= WEEKLY_DUTY_CAP]
        standby = min(len(callable_crews), js_round(standby_pct / 100 * n_crew))
        standby_left, calls = standby, 0

        # --- fly the day ---
        legs = [Leg(l.id, l.tail, l.frm, l.to, l.block, l.sched_dep, l.spoke) for l in template_legs]
        for l in legs:
            l.noise = js_round(rand() * 12)

        state = {du.id: {"eff_start": du.start,
                         "cap": legal_cap - (60 if du.night else 0),
                         "relieved": False} for du in duties}

        disrupted = 0
        for L in sorted(legs, key=lambda l: l.sched_dep):
            du = duty_of[L.id]
            st = state[du.id]

            if du.id not in assigned:
                L.state, L.reason = "CNL", "unstaffed"
                disrupted += 1
                continue
            if ac_loc[L.tail] != L.frm:
                L.state, L.reason = "CNL", "oop"
                disrupted += 1
                continue

            dep = max(L.sched_dep, ac_ready[L.tail]) + L.noise
            if fog and dep < 500:
                dep += 10 + js_round(rand() * 18)
            # Hub congestion feedback: disruption absorbs gates, ground staff and
            # attention, slowing everything still flying. This is what makes a
            # meltdown feed on itself rather than settle down.
            dep += js_round(cfg.congestion * disrupted / len(legs))
            if L.frm == HUB and dep > L.sched_dep + 20:
                dep += 5

            if (dep + L.block + DUTY_POST) - st["eff_start"] > st["cap"]:
                if not st["relieved"] and standby_left > 0:
                    standby_left -= 1
                    calls += 1
                    st["relieved"] = True
                    st["eff_start"] = dep - DUTY_PRE
                    st["cap"] = legal_cap
                    dep += CALLOUT
                else:
                    L.state, L.reason = "CNL", "fdtl"
                    disrupted += 1
                    continue

            arr = dep + L.block
            L.state = "DLY" if (dep - L.sched_dep) >= 15 else "OK"
            L.dep, L.arr = dep, arr
            ac_loc[L.tail] = L.to
            ac_ready[L.tail] = arr + (prof.turn_hub if L.to == HUB else prof.turn_spoke)
            last_arr[L.tail] = arr

        if record_day == d:
            rec = {"tails": [list(x) for x in tails], "legs": legs}
        if record_all:
            # Recording is observation only: it never touches the RNG or any
            # state the simulation reads, so parity with the reference engine holds.
            all_days.append({"day": d, "legs": legs,
                             "assigned": {du_id: c.id for du_id, c in assigned.items()},
                             "standby": standby, "calls": calls})

        # --- book crew hours ---
        for c in crews:
            c.duty_min[d] = 0
        for du_id, c in assigned.items():
            du = duties[du_id]
            c.duty_min[d] = du.mins
            c.last_end = t0 + du.end
            c.consec += 1
        for c in crews:
            if not c.duty_min[d]:
                c.consec = 0

        # --- overnight: reposition what you can, strand the rest ---
        # Readiness resets because the aircraft sits all night, but POSITION carries
        # over, and an aircraft that landed at 01:00 cannot make a 06:00 departure.
        stranded = repoed = 0
        for t in range(N_TAILS):
            ac_ready[t] = max(DAY_START - 60, last_arr[t] - DAY + cfg.overnight_min)
            last_arr[t] = 0
            if ac_loc[t] != HUB:
                if repoed < repo_cap:
                    ac_loc[t] = HUB
                    repoed += 1
                else:
                    stranded += 1

        cnl = [l for l in legs if l.state == "CNL"]
        ok = sum(1 for l in legs if l.state == "OK")
        flown = len(legs) - len(cnl)
        out.append({
            "day": d, "total": len(legs), "cnl": len(cnl), "ok": ok, "flown": flown,
            "otp": (100 * ok / flown) if flown else 100.0,
            "cnl_pct": 100 * len(cnl) / len(legs),
            "dly": sum(1 for l in legs if l.state == "DLY"),
            "unstaffed": unstaffed, "stranded": stranded, "calls": calls,
            "standby": standby, "callable": len(callable_crews),
            "by_reason": {
                "unstaffed": sum(1 for l in cnl if l.reason == "unstaffed"),
                "fdtl": sum(1 for l in cnl if l.reason == "fdtl"),
                "oop": sum(1 for l in cnl if l.reason == "oop"),
            },
        })

    total = sum(o["total"] for o in out)
    can = sum(o["cnl"] for o in out)
    peak = max(out, key=lambda o: o["cnl_pct"])
    return {
        "days": out, "rec": rec, "all_days": all_days,
        "duty_lines": duties, "duties": len(duties), "crews": n_crew,
        "summary": {"total": total, "cnl": can, "cnl_pct": 100 * can / total,
                    "peak_day": peak["day"], "peak_pct": peak["cnl_pct"]},
    }


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

BASE_SEEDS = tuple(range(101, 121))   # 20 schedules, as used throughout the report


def base_profile(crews_per_ac: float = CALIBRATED_CREWS_PER_AC) -> Profile:
    p = PROFILES["lean"]
    return Profile(p.name, crews_per_ac, p.pairs, p.turn_spoke, p.turn_hub,
                   p.slack_spoke, p.slack_hub, p.max_legs)


def mean(xs) -> float:
    xs = list(xs)
    return sum(xs) / len(xs)


def stdev(xs) -> float:
    xs = list(xs)
    m = mean(xs)
    return math.sqrt(mean([(x - m) ** 2 for x in xs]))


def week_cancel_pct(prof: Profile, *, seeds=BASE_SEEDS, **kw) -> float:
    """Mean cancellation rate over a week, averaged across schedules."""
    return mean(run_season(s, prof, **kw)["summary"]["cnl_pct"] for s in seeds)


def cascade_split(prof: Profile, *, seeds=BASE_SEEDS, **kw) -> dict[str, float]:
    """Direct (crew out of hours) against knock-on (out of position) cancellations."""
    direct = knock = 0.0
    for s in seeds:
        r = run_season(s, prof, **kw)
        for day in r["days"]:
            direct += day["by_reason"]["fdtl"]
            knock += day["by_reason"]["oop"] + day["by_reason"]["unstaffed"]
    direct /= len(seeds)
    knock /= len(seeds)
    return {"direct": direct, "knock_on": knock,
            "multiplier": (direct + knock) / direct if direct else float("nan")}


def shapley(prof: Profile, *, seeds=BASE_SEEDS, fdtl_hours=FITTED_FDTL_HOURS,
            fog_days=DEFAULT_FOG_DAYS, standby_baseline=3.0, **kw) -> dict[str, Any]:
    """Exact Shapley decomposition over four causes.

    Switches, in order: FDTL change, winter fog, roster left un-replanned,
    standby withdrawn. Every switch 'on' is the adverse setting.

    Shapley is the only attribution that is exact, order-independent and sums to
    the total with no residual, which matters here because no single cause does
    anything on its own.
    """
    V: dict[tuple[int, int, int, int], float] = {}
    for m in range(16):
        b = ((m >> 3) & 1, (m >> 2) & 1, (m >> 1) & 1, m & 1)
        V[b] = week_cancel_pct(
            prof, seeds=seeds,
            fdtl_hours=fdtl_hours if b[0] else 0,
            fog_days=fog_days if b[1] else (),
            roster_mode="legacy" if b[2] else "replanned",
            standby_pct=0 if b[3] else standby_baseline, **kw)

    weights = {0: 1 / 4, 1: 1 / 12, 2: 1 / 12, 3: 1 / 4}   # |S|!(n-|S|-1)!/n! for n=4
    phi = [0.0] * 4
    for i in range(4):
        for m in range(16):
            S = [(m >> 3) & 1, (m >> 2) & 1, (m >> 1) & 1, m & 1]
            if S[i]:
                continue
            S2 = S.copy()
            S2[i] = 1
            phi[i] += weights[sum(S)] * (V[tuple(S2)] - V[tuple(S)])

    names = ["FDTL rule change", "Winter fog", "Roster not replanned", "Standby withdrawn"]
    total, base = V[(1, 1, 1, 1)], V[(0, 0, 0, 0)]
    return {"values": dict(zip(names, phi)), "coalitions": V,
            "total": total, "baseline": base,
            "shares": {n: 100 * p / (total - base) for n, p in zip(names, phi)},
            "firm_controllable_share": 100 * (phi[2] + phi[3]) / (total - base)}


def compliance_threshold(prof: Profile, *, fdtl_hours=FITTED_FDTL_HOURS,
                         cfg: StructuralConfig | None = None, seed: int = 11) -> dict[str, Any]:
    """How many crews a compliant roster needs, and the bracket that implies.

    The tighter cap forces more duty lines for identical flying. Sustaining them
    at five days on and one off sets a threshold. the demonstration carrier failed to comply, so it
    sat below; carriers at the 9.1 ratio complied, so 1.20x the demonstration carrier cleared it.

    NOTE: the two bounding facts are sourced, but the threshold they anchor to is
    a product of our own duty-construction rules. Section 9 of the report shows it
    moving between 110 and 180 crews across plausible variants.
    """
    cfg = cfg or StructuralConfig(congestion=FITTED_CONGESTION)
    tails, legs = build_template(seed, prof)
    old = len(roster_duties(tails, legs, prof, BASE_CAP, cfg))
    new = len(roster_duties(tails, legs, prof, BASE_CAP - fdtl_hours * 60, cfg))
    threshold = math.ceil(new * 6 / 5)
    return {"duty_lines_old_cap": old, "duty_lines_new_cap": new,
            "inflation_pct": 100 * (new / old - 1),
            "threshold_crews": threshold,
            "lean_range": (math.ceil(threshold / 1.197), threshold - 1),
            "peer_crews": js_round(prof.crews_per_ac * 9.1 / 7.6 * N_TAILS)}


# --- economics --------------------------------------------------------------

WEEKLY_DEPARTURES = 15014        # [SOURCED] regulator notice
NET_DAILY_CNL = 450              # [SOURCED] reported network cancellations per day
NET_DAILY_DEP = 2145             # [SOURCED] 15,014 / 7
HUB_CNL_PCT = 28.5            # [SOURCED] hub airport report
PILOTS = 5200                    # [SOURCED] parliamentary answer
COST_PER_CANCELLED_LAKH = 13.5   # [SOURCED-derived] 577.2 cr / 4,290 flights
COST_PER_PILOT_LAKH = 50.0       # [INVENTED] assumption, range 40-60

# The hub cancelled at a higher rate than the network did. Applying the hub rate to
# the whole network overstates cost by about a quarter, which an earlier version of
# this analysis did.
HUB_TO_NET_FACTOR = (NET_DAILY_CNL / NET_DAILY_DEP) / (HUB_CNL_PCT / 100)


def hub_to_network_scale(week_legs: int) -> float:
    return (WEEKLY_DEPARTURES / week_legs) * HUB_TO_NET_FACTOR


def total_cost_curve(prof: Profile, *, seeds=BASE_SEEDS, standby_levels=range(0, 26),
                     disrupted_weeks=3.0, cost_per_flight_lakh=COST_PER_CANCELLED_LAKH,
                     cost_per_pilot_lakh=COST_PER_PILOT_LAKH, **kw):
    """Single-period (newsvendor) buffer sizing, in rupees crore per year."""
    week_legs = run_season(seeds[0], prof, standby_pct=0, **kw)["summary"]["total"]
    scale = hub_to_network_scale(week_legs)
    rows = []
    for sb in standby_levels:
        cnl = mean(run_season(s, prof, standby_pct=sb, **kw)["summary"]["cnl"] for s in seeds)
        net = cnl * scale
        carry = sb / 100 * PILOTS * cost_per_pilot_lakh / 100
        disr = disrupted_weeks * net * cost_per_flight_lakh / 100
        rows.append({"standby_pct": sb, "hub_cancellations": cnl,
                     "network_cancellations": net, "carrying_cost_cr": carry,
                     "disruption_cost_cr": disr, "total_cost_cr": carry + disr})
    return rows


def optimal_standby(rows) -> dict[str, Any]:
    return min(rows, key=lambda r: r["total_cost_cr"])
