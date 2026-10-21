"""The AI output contract: what the QA agent must return.

The model authors the findings; code only validates, links and formats them.
Kept out of ``reporting/`` on purpose: the deterministic gate installs only
``requirements.txt``, which does not include pydantic (it arrives with
browser-use on the agent path).
"""
from __future__ import annotations

import json
import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

# Bump when the fields change, so an old report can be told apart from a new one.
SCHEMA_VERSION = 2


class Anomaly(BaseModel):
    """One defect the agent observed, in the agent's own words."""

    title: str = Field(description="Short name of the defect")
    severity: Literal["low", "medium", "high"] = Field(
        description=(
            "high = blocks the scenario or corrupts data; "
            "medium = visibly wrong but you could continue; "
            "low = cosmetic or uncertain"
        )
    )
    scenario_step: int = Field(
        description="Scenario step number (1-based), e.g. 5 for 'enter customer information'"
    )
    agent_step: Optional[int] = Field(
        default=None,
        description="Your own action step number, only if you can count it exactly",
    )
    expected: str = Field(description="What correct behaviour would be")
    observed: str = Field(description="What actually happened, as seen")
    evidence: str = Field(description="Exact field id, value, visible text or URL")
    impact: str = Field(description="Effect on the user")


class QAVerdict(BaseModel):
    """The structured verdict the agent must produce when it calls done()."""

    verdict: Literal["PASS", "FAIL"] = Field(
        description=(
            "PASS only if every scenario step passed and no anomaly was found. "
            "FAIL for anything else: a step failed, a step was not run, an anomaly "
            "was observed, or tooling blocked the run. There is no MIXED verdict."
        )
    )
    summary: str = Field(description="2-4 sentences: what worked, what did not")
    scenario_steps: List[str] = Field(
        default_factory=list,
        description=(
            "Exactly one entry per scenario step, in order, starting with the step "
            'number and using PASS / FAIL / NOT RUN, e.g. "5. Enter info: FAIL - first name changed"'
        ),
    )
    anomalies: List[Anomaly] = Field(default_factory=list)

    @field_validator("verdict", mode="before")
    @classmethod
    def _no_mixed(cls, value):
        """A legacy/rogue 'MIXED' answer is a FAIL, never a third state."""
        if isinstance(value, str) and value.strip().upper() == "MIXED":
            return "FAIL"
        return value


# --------------------------------------------------------------------- parsing
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*$", re.MULTILINE)


def parse_verdict(text: str) -> Optional[QAVerdict]:
    """Tolerant fallback when browser-use did not return structured output.

    Browser Use stores the structured answer as JSON in ``final_result()``, so the
    first attempt is a strict parse. The regex path only exists for models that
    wrapped the JSON in prose or code fences.
    """
    if not text:
        return None

    try:
        return QAVerdict.model_validate_json(text)
    except Exception:
        pass

    body = _FENCE.sub("", text)
    match = re.search(r"\{.*\}", body, re.DOTALL)
    if not match:
        return None
    try:
        return QAVerdict.model_validate(json.loads(match.group(0)))
    except Exception:
        return None


# --------------------------------------------------------------------- adapters
def anomalies_for_report(verdict: QAVerdict, n_steps: int = 0) -> List[dict]:
    """Map the contract to the dict keys ``reporting/`` already renders.

    ``agent_step`` is only trusted inside the real step range: a hallucinated
    number must never highlight the wrong row. ``scenario_step`` is always kept
    because it is what the scenario itself defines.
    """
    out: List[dict] = []
    for anomaly in verdict.anomalies:
        agent_step = anomaly.agent_step
        if not agent_step or not (1 <= agent_step <= n_steps):
            agent_step = None
        out.append(
            {
                "title": anomaly.title,
                "severity": anomaly.severity,
                "scenario_step": anomaly.scenario_step,
                "step": agent_step,
                "description": f"Expected: {anomaly.expected}\nObserved: {anomaly.observed}",
                "expected": anomaly.expected,
                "observed": anomaly.observed,
                "evidence": anomaly.evidence,
                "impact": anomaly.impact,
                "source": "agent",
            }
        )
    return out


def first_valid_agent_step(verdict: QAVerdict, n_steps: int) -> Optional[int]:
    """The first anomaly step that actually exists in the run, or None."""
    for anomaly in verdict.anomalies:
        step = anomaly.agent_step
        if step and 1 <= step <= n_steps:
            return step
    return None


_STEP_NUMBER = re.compile(r"^\s*(\d+)\s*[.)]")
_FAILED = re.compile(r"\bFAIL(?:ED)?\b", re.IGNORECASE)
_PASSED = re.compile(r"\bPASS(?:ED)?\b", re.IGNORECASE)


def first_failed_scenario_step(verdict: QAVerdict) -> Optional[int]:
    """The first scenario step the agent itself marked FAIL, from scenario_steps."""
    for line in verdict.scenario_steps:
        match = _STEP_NUMBER.match(line)
        if match and _FAILED.search(line):
            return int(match.group(1))
    return None


def first_failed_scenario_line(verdict: QAVerdict) -> Optional[str]:
    """The first scenario step line the agent marked FAIL, as the agent wrote it.

    Used as the failed row's assertion text, so a row badged FAILED never carries
    the model's (possibly contradictory) overall summary.
    """
    for line in verdict.scenario_steps:
        match = _STEP_NUMBER.match(line)
        if match and _FAILED.search(line):
            return line.strip()
    return None


def first_not_passed_scenario_line(verdict: QAVerdict) -> Optional[str]:
    """The first scenario step line that is not marked PASS, or None.

    A PASS verdict is only valid when every scenario step is PASS; anything else
    (FAIL, NOT RUN, or a missing marker) contradicts it.
    """
    for line in verdict.scenario_steps:
        if _STEP_NUMBER.match(line) and not _PASSED.search(line):
            return line.strip()
    return None


def failed_step_for_verdict(verdict: QAVerdict, n_steps: int) -> Optional[int]:
    """Pick the agent step to anchor the failure on, using the agent's own verdict.

    The first anomaly in the list is not necessarily the blocking one (a visual
    defect can be reported before the step that actually stopped the scenario), so
    prefer the anomaly attached to the first scenario step the agent marked FAIL.
    Fall back to the first high-severity anomaly, then to the first valid step.
    """
    failed_scenario = first_failed_scenario_step(verdict)
    if failed_scenario is not None:
        for anomaly in verdict.anomalies:
            step = anomaly.agent_step
            if anomaly.scenario_step == failed_scenario and step and 1 <= step <= n_steps:
                return step
    for anomaly in verdict.anomalies:
        step = anomaly.agent_step
        if anomaly.severity == "high" and step and 1 <= step <= n_steps:
            return step
    return first_valid_agent_step(verdict, n_steps)


def unparsed_card(raw: str) -> dict:
    """Run-integrity card: the agent finished but returned no usable verdict."""
    return {
        "title": "No structured verdict returned",
        "severity": "high",
        "description": "The agent finished but did not return the required QAVerdict structure.",
        "evidence": (raw or "(empty)")[:300],
        "impact": "Findings cannot be trusted automatically; review the step timeline.",
        "source": "harness",
    }
