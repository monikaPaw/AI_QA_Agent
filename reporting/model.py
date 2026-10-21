"""Normalised report model shared by BOTH execution paths.

The AI agent path (`agent/run_scenario.py`) and the deterministic regression
path (`tests/`) both describe their run with these objects, so exactly one
Markdown writer and one HTML writer are needed for the whole project.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional

# step / test outcomes
PASSED = "passed"
FAILED = "failed"
SKIPPED = "skipped"
MIXED = "mixed"
NOT_RUN = "not-run"
# Steps executed AFTER the first failure. They are not clean passes: the run was
# already failing, so these rows are the agent trying to recover. Rendered
# distinctly so a green row never implies "verified good" past a failure.
RECOVERY = "recovery"

_TARGET = "https://www.saucedemo.com/"


@dataclass
class StepResult:
    """One step of a scenario and the evidence captured right after it."""

    index: int
    name: str
    status: str = PASSED
    detail: str = ""
    url: str = ""
    screenshot: Optional[str] = None  # path relative to the run directory
    error: str = ""


@dataclass
class TestResult:
    """One executed scenario (a pytest test, or one agent run)."""

    name: str
    outcome: str = FAILED
    duration: float = 0.0
    user: str = ""
    steps: List[StepResult] = field(default_factory=list)
    assertion: str = ""
    traceback: str = ""
    failure_screenshot: Optional[str] = None
    note: str = ""
    failed_step_index: Optional[int] = None
    # The agent's own per-scenario-step assessment (e.g. "5. Enter info: FAIL").
    scenario_steps: List[str] = field(default_factory=list)

    @property
    def failed_step(self) -> Optional[StepResult]:
        # An explicit index wins: for the agent path the meaningful failure is the
        # step where the defect was observed, not the first action that errored.
        if self.failed_step_index is not None:
            for step in self.steps:
                if step.index == self.failed_step_index:
                    return step
        for step in self.steps:
            if step.status == FAILED:
                return step
        if self.outcome == FAILED and self.steps:
            return self.steps[-1]
        return None

    @property
    def steps_passed(self) -> int:
        return sum(1 for s in self.steps if s.status == PASSED)


def _is_workaround(name: str) -> bool:
    """A step that is not a user-level interaction (currently: raw JS `evaluate`).

    Used only to TAG such steps in the report, never to hide them: the agent may
    legitimately read page state, but forcing values via JavaScript is not
    something a real user can do, so it must not look like ordinary testing.
    """
    return name.strip().lower().startswith("evaluate")


def apply_recovery_status(test: "TestResult") -> list:
    """Return the steps of `test` with post-failure rows re-labelled for display.

    A step's stored status only records whether its browser action raised an
    error, so in the agent path every cleanly executed step stays `PASSED` - even
    the ones that only ran because an earlier step had already failed. Reading
    those as "passed" is misleading, so:

    - the FIRST failed step stays `FAILED` (the anchor where the defect showed),
    - every step AFTER that anchor becomes `RECOVERY`,
    - a JS `evaluate` at or after the anchor is additionally flagged `workaround`.

    The anchor is ``failed_step_index`` when the caller knows it (the AI path sets
    it from the model's verdict). Falling back to the first `FAILED` row would let
    a transient model/API error in the middle of a run re-label every later real
    step as RECOVERY.

    Returns lightweight copies so the original report data is untouched. The
    deterministic pytest path never hits this: its post-failure steps are already
    `NOT_RUN`, not `PASSED`.
    """
    anchor = test.failed_step_index
    if anchor is None:
        anchor = next((s.index for s in test.steps if s.status == FAILED), None)
    out = []
    for step in test.steps:
        status = step.status
        workaround = False
        # Only cleanly-executed ("passed") rows get re-labelled. Explicitly
        # skipped/not-run rows keep their own meaning ("never reached").
        if anchor is not None and step.index > anchor and step.status == PASSED:
            status = RECOVERY
            workaround = _is_workaround(step.name)
        out.append({
            "index": step.index,
            "name": step.name,
            "status": status,
            "detail": step.detail,
            "url": step.url,
            "screenshot": step.screenshot,
            "error": step.error,
            "workaround": workaround,
        })
    return out


@dataclass
class RunReport:
    """A whole run: one or more scenarios executed by one path."""

    run_id: str
    mode: str  # "ai-agent" | "regression"
    framework: str = ""
    target: str = _TARGET
    tests: List[TestResult] = field(default_factory=list)
    generated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    scenarios: List[str] = field(default_factory=list)
    model: str = ""
    provider: str = ""
    summary: str = ""
    what_it_did: str = ""
    automated_vs_human: str = ""
    anomalies: List[dict] = field(default_factory=list)
    # Code-authored facts about the run itself (budget stop, false success,
    # unparsed verdict). Kept apart from `anomalies`, which the AI authors.
    integrity: List[dict] = field(default_factory=list)

    # -------------------------------------------------------------- derived
    @property
    def passed(self) -> int:
        return sum(1 for t in self.tests if t.outcome == PASSED)

    @property
    def failed(self) -> int:
        return sum(1 for t in self.tests if t.outcome == FAILED)

    @property
    def skipped(self) -> int:
        return sum(1 for t in self.tests if t.outcome == SKIPPED)

    @property
    def issues(self) -> int:
        """Tests that need attention: outright failures plus 'mixed' runs."""
        return sum(1 for t in self.tests if t.outcome in (FAILED, MIXED))

    @property
    def duration(self) -> float:
        return sum(t.duration for t in self.tests)

    @property
    def verdict(self) -> str:
        if self.failed:
            return "FAIL"
        if any(t.outcome == MIXED for t in self.tests):
            return "MIXED"
        if self.tests and not self.passed:
            return "INCOMPLETE"
        return "PASS"

    @property
    def ok(self) -> bool:
        return self.failed == 0
