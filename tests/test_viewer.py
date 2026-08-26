#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
# Copyright (c) 2026 Sundaravalli Narayanaswami and Vaibhav Sharma
"""
Viewer tests.

The viewer is a renderer: it draws a trace and computes nothing of its own. The
only thing worth asserting about it, then, is that what appears on screen is
what the trace says. Every check here compares rendered text against the trace
that produced it, so a change to the drawing code that quietly alters a number
fails the build.

    python tests/test_viewer.py

Needs a browser, which the other suites do not:

    pip install playwright && playwright install chromium

Without it the suite skips rather than fails, so a contributor who only touches
the engine is not forced to install a browser.
"""
from __future__ import annotations

import json
import pathlib
import subprocess
import sys
import tempfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

GREEN, RED, DIM, OFF = "\033[32m", "\033[31m", "\033[2m", "\033[0m"
FAILURES: list[str] = []
CHECKS = 0


def check(condition: bool, description: str) -> None:
    global CHECKS
    CHECKS += 1
    if condition:
        print(f"  {GREEN}ok{OFF}   {description}")
    else:
        print(f"  {RED}FAIL{OFF} {description}")
        FAILURES.append(description)


def build_page(tmp: pathlib.Path, config: str) -> tuple[pathlib.Path, dict]:
    """Run a configuration, bundle its trace, return the page and the trace."""
    trace = tmp / "a.trace.json"
    subprocess.run([sys.executable, str(ROOT / "run.py"), str(ROOT / config),
                    "--trace", str(trace)],
                   check=True, stdout=subprocess.DEVNULL)
    page = tmp / "a.html"
    subprocess.run([sys.executable, str(ROOT / "viewer" / "bundle.py"),
                    str(trace), "-o", str(page)],
                   check=True, stdout=subprocess.DEVNULL)
    return page, json.loads(trace.read_text(encoding="utf-8"))


def run_checks(pw, tmp: pathlib.Path) -> None:
    page_path, trace = build_page(tmp, "configs/indigo_bom.yaml")
    metrics = trace["metrics"]

    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1300})
    page.goto(page_path.resolve().as_uri())
    page.wait_for_timeout(1500)

    body = page.inner_text("body")

    # --- the trace loads at all -------------------------------------------
    check("AIRRESILIENCE" in body.upper(), "viewer loads an embedded trace")
    check(page.locator("#panes .card").count() == 1,
          "one trace produces one pane")

    # --- run metadata is the trace's, not a placeholder --------------------
    check(str(trace["meta"]["seed"]) in body, "seed shown matches the trace")
    check(str(len(trace["resources"]["aircraft"])) in body,
          "aircraft count shown matches the trace")
    check(str(trace["network"]["hub"]) in body, "hub shown matches the trace")

    # --- week summary -------------------------------------------------------
    week_pct = f"{metrics['cancel_pct']:.1f}%"
    check(week_pct in body, f"week cancellation rate rendered as {week_pct}")
    check(f"{metrics['legs']:,}" in body or str(metrics["legs"]) in body,
          "leg count rendered")

    # --- one tab per day, labelled from the trace ---------------------------
    tabs = page.locator("#days button")
    check(tabs.count() == len(trace["days"]),
          f"one day tab per day ({len(trace['days'])})")
    first_label = trace["days"][0]["label"].split(" ")[0]
    check(tabs.first.inner_text().strip() == first_label,
          "day tabs take their labels from the trace")

    # --- the day panel agrees with that day's metrics -----------------------
    day0 = trace["days"][0]["metrics"]
    check(str(day0["cancelled"]) in body,
          "first day's cancellations match the trace")

    # --- switching day changes what is drawn --------------------------------
    before = page.inner_text("body")
    worst = max(range(len(trace["days"])),
                key=lambda i: trace["days"][i]["metrics"]["cancelled"])
    tabs.nth(worst).click()
    page.wait_for_timeout(700)
    after = page.inner_text("body")
    check(before != after, "selecting a different day redraws the page")
    check(str(trace["days"][worst]["metrics"]["cancelled"]) in after,
          "the selected day's cancellations are rendered")

    # --- cancellation reasons are the trace's own ---------------------------
    for reason, count in metrics["by_reason"].items():
        check(str(count) in after,
              f"reason count rendered for {reason} ({count})")
        check(reason in after.replace(" ", "_").lower()
              or reason.replace("_", " ") in after.lower(),
              f"reason named: {reason}")

    # --- scrubbing moves the clock without redrawing from scratch -----------
    page.evaluate("""() => {
        const s = document.querySelector('#scrub');
        s.value = 900;
        s.dispatchEvent(new Event('input', {bubbles: true}));
    }""")
    page.wait_for_timeout(500)
    check(page.inner_text("body") != after, "scrubbing advances the view")

    browser.close()

    # --- a second trace produces a comparison, not a mess -------------------
    trace_b = tmp / "b.trace.json"
    subprocess.run([sys.executable, str(ROOT / "run.py"),
                    str(ROOT / "configs/indigo_bom.yaml"),
                    "--standby", "12", "--trace", str(trace_b)],
                   check=True, stdout=subprocess.DEVNULL)
    page_two = tmp / "two.html"
    subprocess.run([sys.executable, str(ROOT / "viewer" / "bundle.py"),
                    str(tmp / "a.trace.json"), str(trace_b), "-o", str(page_two)],
                   check=True, stdout=subprocess.DEVNULL)

    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1300})
    page.goto(page_two.resolve().as_uri())
    page.wait_for_timeout(1500)
    check(page.locator("#panes .card").count() == 2,
          "two traces produce two panes")
    b_metrics = json.loads(trace_b.read_text(encoding="utf-8"))["metrics"]
    body2 = page.inner_text("body")
    check(f"{b_metrics['cancel_pct']:.1f}%" in body2,
          "the second trace's own rate is rendered, not the first's")
    check(b_metrics["cancel_pct"] < metrics["cancel_pct"],
          "standby cover reduces cancellations (guards the fixture itself)")
    browser.close()

    # --- a different topology renders too -----------------------------------
    page_p2p, trace_p2p = build_page(tmp, "configs/example_p2p.yaml")
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1400, "height": 1300})
    page.goto(page_p2p.resolve().as_uri())
    page.wait_for_timeout(1500)
    body3 = page.inner_text("body")
    check(str(trace_p2p["network"]["hub"]) in body3,
          "a point-to-point trace renders with its own base")
    check(page.locator("#days button").count() == len(trace_p2p["days"]),
          "day tabs follow the configuration, not a fixed week")
    browser.close()


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"{DIM}playwright is not installed; skipping viewer tests.{OFF}")
        print(f"{DIM}  pip install playwright && playwright install chromium{OFF}")
        return 0

    print("viewer: rendering real traces in a real browser\n")
    with tempfile.TemporaryDirectory() as d:
        try:
            with sync_playwright() as pw:
                run_checks(pw, pathlib.Path(d))
        except Exception as exc:  # noqa: BLE001
            if "Executable doesn't exist" in str(exc) or "playwright install" in str(exc):
                print(f"{DIM}no browser installed; skipping viewer tests.{OFF}")
                print(f"{DIM}  playwright install chromium{OFF}")
                return 0
            raise

    print()
    if FAILURES:
        print(f"{RED}FAILED{OFF}  {len(FAILURES)} of {CHECKS} checks")
        return 1
    print(f"{GREEN}PASSED{OFF}  all {CHECKS} checks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
