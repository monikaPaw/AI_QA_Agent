"""Markdown report writer for BOTH paths (single source of report formatting).

Produces the AI QA report format:
  Executive Summary -> Failures Found -> Step-by-Step Timeline -> Evidence
Screenshots are referenced by relative path; the HTML writer embeds them.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from .model import (
    FAILED,
    NOT_RUN,
    PASSED,
    RECOVERY,
    SKIPPED,
    RunReport,
    TestResult,
    apply_recovery_status,
)


def _outcome_label(outcome: str) -> str:
    return {
        PASSED: "PASSED",
        FAILED: "FAILED",
        SKIPPED: "SKIPPED",
        RECOVERY: "RECOVERY",
        NOT_RUN: "NOT RUN",
    }.get(outcome, outcome.upper())


def render_markdown(report: RunReport) -> str:
    md: list[str] = []
    md.append("# SauceDemo QA Test Report")
    md.append(f"**Run ID:** {report.run_id}")
    md.append(f"**Generated:** {report.generated_at}")
    md.append(f"**Mode:** {report.mode}")
    md.append("")

    # ------------------------------------------------------------ summary
    md.append("## Executive Summary")
    md.append(f"- **Verdict:** {report.verdict}")
    if report.framework:
        md.append(f"- **Framework:** {report.framework}")
    md.append(f"- **Target:** {report.target}")
    if report.model:
        md.append(f"- **Model:** {report.model}")
    if report.provider:
        md.append(f"- **Provider:** {report.provider}")
    md.append(f"- **Tests Passed:** {report.passed}")
    md.append(f"- **Tests Failed:** {report.failed}")
    if report.skipped:
        md.append(f"- **Tests Skipped:** {report.skipped}")
    md.append(f"- **Completed Successfully:** {report.verdict == 'PASS'}")
    md.append(f"- **Total Test Duration:** {report.duration:.2f}s")
    md.append("")

    md.append("### Summary")
    if report.summary:
        md.append(report.summary)
    md.append("")
    md.append("TESTS EXECUTED:")
    for test in report.tests:
        line = f"- {_outcome_label(test.outcome)}: `{test.name}` ({test.duration:.2f}s)"
        if test.outcome == FAILED:
            step = test.failed_step
            if step:
                line += f" at step {step.index} ({step.name})"
            if test.assertion:
                line += f" - {test.assertion}"
        md.append(line)
    md.append("")
    if report.failed:
        md.append(
            f"VERDICT: {report.verdict} - one or more checks did not succeed; "
            "see Failures Found below."
        )
    else:
        md.append(f"VERDICT: {report.verdict} - the scenario completed with no detected failures.")
    md.append("")

    # drop what-it-did / automated-vs-human sections (removed per request)

    if report.anomalies:
        md.append("## Anomalies Found")
        md.append(
            "*Reported by the AI agent in its own words; code only validates the "
            "structure and links the evidence.*"
        )
        md.append("")
        for i, a in enumerate(report.anomalies, 1):
            md.append(f"### {i}. {a.get('title', 'Anomaly')}")
            md.append(f"- **Severity:** {a.get('severity', 'medium')}")
            if a.get("scenario_step"):
                md.append(f"- **Scenario step:** {a['scenario_step']}")
            if a.get("step"):
                md.append(f"- **Agent step:** {a['step']}")
            if a.get("expected"):
                md.append(f"- **Expected:** {a['expected']}")
            if a.get("observed"):
                md.append(f"- **Observed:** {a['observed']}")
            if not a.get("expected") and not a.get("observed"):
                md.append(f"- **Description:** {a.get('description', '')}")
            if a.get("evidence"):
                md.append(f"- **Evidence:** {a['evidence']}")
            if a.get("impact"):
                md.append(f"- **Impact:** {a['impact']}")
            md.append("")

    if report.integrity:
        md.append("## Run Integrity")
        md.append("*Objective checks on the run itself, not product findings.*")
        md.append("")
        for i, a in enumerate(report.integrity, 1):
            md.append(f"### {i}. {a.get('title', 'Run check')}")
            md.append(f"- **Severity:** {a.get('severity', 'medium')}")
            md.append(f"- **Description:** {a.get('description', '')}")
            if a.get("evidence"):
                md.append(f"- **Evidence:** {a['evidence']}")
            if a.get("impact"):
                md.append(f"- **Impact:** {a['impact']}")
            md.append("")

    # ----------------------------------------------------------- failures
    failures = [t for t in report.tests if t.outcome == FAILED]
    md.append("## Failures Found")
    if not failures:
        md.append("None detected.")
        md.append("")
    else:
        for i, test in enumerate(failures, 1):
            step = test.failed_step
            md.append(f"### {i}. {test.name}")
            md.append("- **Severity:** high")
            md.append(f"- **Description:** {test.assertion or 'Test failed'}")
            if step:
                total = len([s for s in test.steps]) or step.index
                md.append(f"- **Failed Step:** step {step.index} of {total} - {step.name}")
                if step.detail:
                    md.append(f"- **Step Evidence:** {step.detail}")
                if step.screenshot:
                    md.append(f"- **Screenshot:** {step.screenshot}")
            if test.failure_screenshot:
                md.append(f"- **Screenshot at the moment of failure:** {test.failure_screenshot}")
            md.append(f"- **Duration:** {test.duration:.2f}s")
            md.append(
                "- **Impact:** the scenario could not be completed as expected for "
                "this user account."
            )
            md.append("")
            if step and step.screenshot:
                md.append(f"**Screenshot of the failed step (step {step.index} - {step.name}):**")
                md.append("")
                md.append(f"![Failed step {step.index} - {step.name}]({step.screenshot})")
                md.append("")

    # ---------------------------------------------------------- timeline
    md.append("## Step-by-Step Timeline")
    for i, test in enumerate(report.tests, 1):
        md.append(
            f"### Test {i}: {test.name} - {_outcome_label(test.outcome)} ({test.duration:.2f}s)"
        )
        if test.scenario_steps:
            md.append("#### Scenario steps (agent's own assessment)")
            for line in test.scenario_steps:
                md.append(f"- {line}")
            md.append("")
        if not test.steps:
            md.append("*No scenario steps were executed (test failed during setup).*")
            md.append("")
            continue
        steps = apply_recovery_status(test)
        if any(s["status"] == RECOVERY for s in steps):
            md.append(
                "*Legend: `RECOVERY` = executed after the first failure, while the run was "
                "already failing - not a verified pass. `NON-USER WORKAROUND` = raw JavaScript "
                "used to force a value (a real user could not do this).*"
            )
            md.append("")
        for s in steps:
            marker = ""
            if s["status"] == FAILED:
                marker = " **<-- FAILED**"
            elif s["status"] == RECOVERY:
                marker = " **<-- RECOVERY**"
            if s["workaround"]:
                marker += " **<-- NON-USER WORKAROUND (JS)**"
            md.append(f"#### Step {s['index']}: {s['name']}{marker}")
            if s["detail"]:
                md.append(f"**Observation:** {s['detail']}")
            if s["error"]:
                md.append(f"**Assertion:** {s['error']}")
            if s["url"]:
                md.append(f"**URL:** {s['url']}")
            if s["screenshot"]:
                md.append(f"**Screenshot:** `{s['screenshot']}`")
            md.append("")
        md.append("")

    # ----------------------------------------------------------- evidence
    captured = sum(1 for t in report.tests for s in t.steps if s.screenshot)
    md.append("## Evidence")
    md.append(f"### Step Screenshots Captured: {captured}")
    md.append("")
    if failures:
        md.append("### Failure Tracebacks")
        for test in failures:
            md.append(f"#### `{test.name}`")
            md.append("```text")
            md.append((test.traceback or test.assertion).rstrip())
            md.append("```")
            md.append("")
            step = test.failed_step
            if step and step.screenshot:
                md.append(f"![Failed step {step.index} - {step.name}]({step.screenshot})")
                md.append("")

    # ---------------------------------------------------------- closing
    md.append("---")
    md.append("*Generated by saucedemo-ai-qa-agent*")
    return "\n".join(md)


def write_markdown(report: RunReport, out_dir: Path, filename: str = "report.md") -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(render_markdown(report), encoding="utf-8")
    return path
