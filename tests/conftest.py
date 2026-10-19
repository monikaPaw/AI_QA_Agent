"""pytest fixtures + reporting for the deterministic SauceDemo regression suite.

This is the *fallback / gate* path: it needs no model, no API key and no
browser-use - only pytest + Playwright.

Screenshots:
- one screenshot is captured after every scenario step -> reports/pytest/<run>/steps/
- one screenshot is captured at the moment a test fails
- the HTML report shows every step with its own PASS/FAIL badge, its screenshot
  and its observation/assertion in the same row; the failed step is highlighted
- screenshots are embedded in the HTML by default (use --no-embed for file refs)
"""
from __future__ import annotations

import re
import shutil
import sys
import tempfile
import traceback
from datetime import datetime
from pathlib import Path

import pytest
from playwright.sync_api import sync_playwright

TESTS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = TESTS_DIR.parent

# make `reporting` (project root) and `steps` (tests dir) importable
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(TESTS_DIR))

from reporting import (  # noqa: E402
    FAILED,
    MIXED,
    NOT_RUN,
    PASSED,
    RunReport,
    StepResult,
    TestResult,
    write_html,
    write_markdown,
)
from steps import step_helpers  # noqa: E402
from tools.chrome_profile import seed_password_prefs  # noqa: E402

RUN_STARTED = datetime.now()
RUN_ID = RUN_STARTED.strftime("%Y-%m-%d_%H-%M-%S")
OUT_DIR = PROJECT_ROOT / "reports" / "pytest" / RUN_ID
STEPS_DIR = OUT_DIR / "steps"
SCENARIO = "checkout: login -> add Backpack + Bike Light -> cart -> checkout form -> complete order"

_RESULTS: list[TestResult] = []


# ------------------------------------------------------------------ options
def pytest_addoption(parser):
    group = parser.getgroup("saucedemo")
    group.addoption("--embed", dest="embed", action="store_true", default=True,
                    help="embed screenshots in index.html as base64 (default)")
    group.addoption("--no-embed", dest="embed", action="store_false",
                    help="keep screenshots as files next to index.html")


def _headed(config) -> bool:
    """--headed is provided by pytest-playwright; be tolerant if it is absent."""
    try:
        return bool(config.getoption("--headed"))
    except (ValueError, AttributeError):
        return False


# ----------------------------------------------------------------- helpers
def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _short_name(nodeid: str) -> str:
    return nodeid.split("::")[-1].replace("[chromium]", "")


def _user_for(nodeid: str) -> str:
    for user in ("problem_user", "standard_user", "locked_out_user", "performance_glitch_user",
                 "error_user", "visual_user"):
        if user in nodeid:
            return user
    return ""


def _assertion_message(details: str) -> str:
    m = re.search(r"AssertionError:\s*(.*)", details)
    if m:
        return m.group(1).strip()
    lines = [ln for ln in details.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _build_steps(recorded: list, outcome: str, assertion: str) -> list[StepResult]:
    steps = [
        StepResult(
            index=int(s.get("index", i + 1)),
            name=s.get("name", f"Step {i + 1}"),
            detail=s.get("detail", ""),
            url=s.get("url", ""),
            screenshot=s.get("screenshot"),
        )
        for i, s in enumerate(recorded or [])
    ]
    if outcome == FAILED and steps:
        steps[-1].status = FAILED
        steps[-1].error = assertion
        # show the steps that were never reached
        for i, name in enumerate(step_helpers.SCENARIO_STEPS[len(steps):], start=len(steps) + 1):
            steps.append(StepResult(index=i, name=name, status=NOT_RUN,
                                    detail="not reached - the test failed at an earlier step"))
    return steps


# ---------------------------------------------------------------- fixtures
@pytest.fixture
def page(request):
    with sync_playwright() as p:
        headless = not _headed(request.config)
        # Chrome's password-manager popups ("Change your password" breach
        # warning + save-password bubble) cover the page and swallow clicks in
        # headed runs, so each test gets its own fresh profile with those prefs
        # switched off - fresh per test keeps the cookie/cart isolation the old
        # new_context() gave.
        profile_dir = Path(tempfile.mkdtemp(prefix="saucedemo-pytest-profile-"))
        seed_password_prefs(profile_dir)
        try:
            context = p.chromium.launch_persistent_context(
                str(profile_dir), headless=headless, viewport={"width": 1280, "height": 900}
            )
        except BaseException:
            shutil.rmtree(profile_dir, ignore_errors=True)
            raise
        page = context.pages[0] if context.pages else context.new_page()
        prefix = _slug(_short_name(request.node.nodeid))

        def screenshotter(index: int, name: str):
            fname = f"{prefix}__step_{index:02d}_{_slug(name)}.png"
            try:
                STEPS_DIR.mkdir(parents=True, exist_ok=True)
                page.screenshot(path=str(STEPS_DIR / fname), full_page=False)
                return f"steps/{fname}"
            except Exception:
                return None

        step_helpers.clear_steps()
        step_helpers.set_screenshotter(screenshotter)
        yield page
        step_helpers.set_screenshotter(None)
        context.close()  # a persistent context owns the browser it launched
        shutil.rmtree(profile_dir, ignore_errors=True)


# ------------------------------------------------------------------ hooks
@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    rep = outcome.get_result()

    if rep.when == "setup" and rep.failed:
        _RESULTS.append(
            TestResult(
                name=_short_name(item.nodeid),
                outcome=FAILED,
                duration=getattr(rep, "duration", 0.0),
                user=_user_for(item.nodeid),
                traceback=str(rep.longrepr),
                assertion="test failed during setup",
                note="No scenario steps were executed (test failed during setup).",
            )
        )
    elif rep.when == "call":
        outcome_value = FAILED if rep.failed else (PASSED if rep.passed else "skipped")
        assertion = _assertion_message(str(rep.longrepr)) if rep.failed else ""
        test = TestResult(
            name=_short_name(item.nodeid),
            outcome=outcome_value,
            duration=getattr(rep, "duration", 0.0),
            user=_user_for(item.nodeid),
            steps=_build_steps(step_helpers.get_steps(), outcome_value, assertion),
            assertion=assertion,
            traceback=str(rep.longrepr) if rep.failed else "",
        )
        if rep.failed:
            page = item.funcargs.get("page")
            if page is not None:
                fname = f"{_slug(_short_name(item.nodeid))}_failed.png"
                try:
                    OUT_DIR.mkdir(parents=True, exist_ok=True)
                    page.screenshot(path=str(OUT_DIR / fname), full_page=False)
                    test.failure_screenshot = fname
                except Exception:
                    test.failure_screenshot = None
        _RESULTS.append(test)


def _build_report() -> RunReport:
    failed = sum(1 for r in _RESULTS if r.outcome == FAILED)
    report = RunReport(
        run_id=RUN_ID,
        mode="regression",
        framework="pytest + Playwright (Python)",
        tests=list(_RESULTS),
        scenarios=[SCENARIO],
    )
    report.summary = (
        "COMPLETION STATUS: "
        + ("All scenario tests passed." if not failed else "One or more scenario tests FAILED.")
        + "\n\nThe same 7-step checkout scenario is executed once per user account. "
        "A screenshot is captured after every step; each step is shown below with its own "
        "result and evidence, so the exact point of failure is visible."
    )

    # derive from results
    anomalies = []
    for r in _RESULTS:
        if r.outcome == FAILED and r.assertion:
            anomalies.append({
                "title": "Test failure",
                "severity": "high",
                "description": r.assertion,
                "evidence": (r.traceback or r.note or ""),
                "impact": "Blocks completing the scenario as expected.",
            })
        elif r.outcome == MIXED:
            anomalies.append({
                "title": "Mixed result",
                "severity": "medium",
                "description": r.assertion or r.note or "Mixed outcome",
                "evidence": "See step timeline.",
                "impact": "Needs review.",
            })

    # simple dedupe
    seen = set()
    uniq = []
    for a in anomalies:
        if a["title"] not in seen:
            seen.add(a["title"]); uniq.append(a)
    report.anomalies = uniq
    return report


def pytest_sessionfinish(session, exitstatus):
    if not _RESULTS:
        return
    terminal = session.config.pluginmanager.get_plugin("terminalreporter")

    def say(message: str) -> None:
        if terminal:
            terminal.write_line(message)

    try:
        report = _build_report()
        embed = session.config.getoption("embed")
        md_path = write_markdown(report, OUT_DIR)
        html_path = write_html(report, OUT_DIR, embed=embed)
        say("")
        say(f"Verdict: {report.verdict}  ({report.passed} passed, {report.failed} failed)")
        say(f"Report (Markdown): {md_path}")
        say(f"Report (HTML):     {html_path}")
    except Exception as exc:  # never break the test run because of reporting
        say(f"\n[report] generation failed: {exc}")
        say(traceback.format_exc())
