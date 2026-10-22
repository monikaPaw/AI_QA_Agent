#!/usr/bin/env python3
"""AI-first path: run a scenario with the Browser Use agent.

Usage:
    python agent/run_scenario.py --scenario scenarios/checkout_problem_user.md
    python agent/run_scenario.py --all
    python agent/run_scenario.py --scenario scenarios/checkout_problem_user.md --no-embed

Speed / demo options:
    --budget 420      hard wall-clock cap in seconds; the run always ends with a report
    --max-steps 15    override the step cap
    --headless        run without a visible browser window

Deterministic-test options:
    --generate-test   after the run, freeze the scenario into a pytest file (only if it changed)
    --run-test        also run that generated pytest file (implies --generate-test)

The agent performs the login itself (scenario step 1), like a real user.

This path needs a model/API key (see MODEL_ACCESS.md) and `requirements-agent.txt`.
It writes its report to reports/agent/<timestamp>/ (report.md + index.html).

The agent decides the verdict and authors the findings (see agent/qa_verdict.py).
This script only validates them, links the evidence and renders the report;
code-authored run facts are reported separately under "Run Integrity". The
deterministic pytest suite remains the gate of record for a known scenario.
"""
import argparse
import json
import re
import shutil
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from agent.qa_verdict import (  # noqa: E402
    QAVerdict,
    anomalies_for_report,
    failed_step_for_verdict,
    first_failed_scenario_line,
    first_not_passed_scenario_line,
    parse_verdict,
    unparsed_card,
)
from config.load_model_access import ModelAccessConfig  # noqa: E402
from reporting import (  # noqa: E402
    FAILED,
    PASSED,
    RunReport,
    StepResult,
    TestResult,
    write_html,
    write_markdown,
)

TARGET = "https://www.saucedemo.com/"
SCENARIOS_DIR = PROJECT_ROOT / "scenarios"
SCENARIO_GLOB = "*.md"



def _safe(fn, default=None):
    try:
        value = fn()
        return default if value is None else value
    except Exception:
        return default


def load_scenario(path: Path) -> tuple[str, str]:
    """Return (title, text) of a scenario file."""
    text = path.read_text(encoding="utf-8")
    m = re.search(r"^#\s*(.+)$", text, re.MULTILINE)
    title = (m.group(1) if m else path.stem).replace("Scenario:", "").strip()
    return title, text


def scenario_user(text: str) -> str:
    m = re.search(r"\*\*User:\*\*\s*([^\s*]+)", text)
    return m.group(1) if m else ""


def start_actions() -> list:
    """The single navigation action the agent should start from.

    Note: browser-use treats ``navigate`` as a sequence-terminating action, so it
    must be the ONLY entry - anything after it is skipped. This opens the login
    page and the agent logs in itself.
    """
    return [{"navigate": {"url": TARGET}}]


def build_task(title: str, text: str) -> str:
    # Lines starting with '>' are human-only notes (e.g. "this user is known to
    # be broken"). Dropping them keeps the task short and avoids leaking the
    # expected answer to the agent, so its verdict is its own.
    body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith(">"))
    return (
        f"Execute this test scenario end-to-end on {TARGET} and act as the QA agent.\n\n"
        f"{body}\n\n"
        "Behaviour rules:\n"
        "- On the login page, identify the username field, password field and login button from "
        "the page state, then use them.\n"
        "- After typing into a field, read the field's actual value back and verify it matches "
        "what you intended before moving on.\n"
        "- After adding items, verify the cart badge count.\n"
        "- Verify each navigation/click had the expected effect.\n"
        "- In the evaluate action, always wrap your code in an IIFE and return from inside it, "
        "e.g. (function(){ const el = document.getElementById('x'); return el.value; })(). "
        "A bare top-level `return` is a JavaScript error and wastes a step.\n"
        "- If the same obstacle blocks you after two attempts, stop trying workarounds: record the "
        "evidence, finish the run with done(success=false, ...), and describe exactly what you "
        "observed. Do not burn steps on repeated retries.\n"
        "- Record every anomaly in the structured verdict: scenario step, expected, observed, evidence.\n"
        "- Set success=true ONLY if you found no anomalies and every scenario step passed."
    )


def _action_label(action) -> str:
    """Short readable label for one browser-use action."""
    try:
        if hasattr(action, "model_dump"):
            data = action.model_dump(exclude_none=True, mode="json")
        elif isinstance(action, dict):
            data = dict(action)
        else:
            return str(action)
    except Exception:
        return str(action)
    data.pop("interacted_element", None)
    if len(data) == 1:
        name, value = next(iter(data.items()))
        try:
            return f"{name}({json.dumps(value)})"
        except Exception:
            return f"{name}({value})"
    return json.dumps(data)


# ------------------------------------------------------------------ scenario
_STEPS_SECTION = re.compile(
    r"^##\s+Steps\s*$(.*?)(?=^##\s+|\Z)", re.MULTILINE | re.DOTALL | re.IGNORECASE
)
_STEP_LINE = re.compile(r"^\s*(\d+)[.)]\s+(.+)$")
_REPORT_HINT = re.compile(r"^\s*>\s*report-match\s*:\s*(\d+)\s*=\s*(.+)$", re.IGNORECASE)


def parse_scenario_steps(text: str) -> list:
    """The scenario's numbered steps as ``[(number, text), ...]``.

    These are the names the report shows instead of raw browser actions.
    """
    section = _STEPS_SECTION.search(text)
    body = section.group(1) if section else text
    steps = []
    for line in body.splitlines():
        match = _STEP_LINE.match(line)
        if match:
            step_text = match.group(2).strip().rstrip(".")
            # Scenario steps use Markdown emphasis (**Backpack**); the timeline
            # shows plain text, so drop the markers instead of printing them.
            step_text = re.sub(r"[*_`]", "", step_text)
            steps.append((int(match.group(1)), step_text))
    return steps


def parse_report_hints(text: str) -> dict:
    """Human-only ``> report-match: N = keyword, ...`` lines from the scenario.

    They map browser elements to scenario steps for the report timeline. As with
    every ``>`` line, they are stripped before the agent sees the task.
    """
    hints: dict = {}
    for line in text.splitlines():
        match = _REPORT_HINT.match(line)
        if not match:
            continue
        step = int(match.group(1))
        keywords = [k.strip() for k in match.group(2).split(",") if k.strip()]
        hints.setdefault(step, []).extend(keywords)
    return hints


# Fallback when a scenario has no report-match hints (identical to the two
# checkout scenarios in this repo). Longest keyword wins, so "Add to cart"
# (step 2) beats "cart" (step 3).
_DEFAULT_STEP_MATCH = {
    1: ("username", "password", "login"),
    2: ("add to cart",),
    3: ("cart",),
    4: ("checkout",),
    5: ("first name", "last name", "postal", "zip"),
    6: ("continue",),
    7: ("finish",),
}


def _action_data(action) -> dict:
    try:
        if hasattr(action, "model_dump"):
            return action.model_dump(exclude_none=True, mode="json")
        if isinstance(action, dict):
            return dict(action)
    except Exception:
        pass
    return {}


def _action_name(action) -> str:
    data = _action_data(action)
    return next(iter(data)) if len(data) == 1 else ""


def _action_value(action) -> dict:
    data = _action_data(action)
    if len(data) != 1:
        return {}
    value = next(iter(data.values()))
    return value if isinstance(value, dict) else {}


def _element_label(element) -> str:
    """Human name of the element an action touched (e.g. 'Login', 'First Name').

    browser-use stores this in the step's ``interacted_element``; without it the
    report would only have the numeric index from the action JSON.
    """
    if element is None:
        return ""
    ax_name = getattr(element, "ax_name", None)
    if ax_name:
        return str(ax_name).strip()
    attributes = getattr(element, "attributes", None) or {}
    if isinstance(attributes, dict):
        for key in ("aria-label", "placeholder", "name", "text", "id", "value"):
            value = attributes.get(key)
            if value:
                return str(value).strip()
    value = getattr(element, "node_value", None)
    if value:
        return str(value).strip()
    return str(getattr(element, "node_name", "") or "").strip()


def _human_action(action, element) -> str:
    """Readable label: ``click "Login"`` instead of ``click({"index": 6})``."""
    name = _action_name(action)
    value = _action_value(action)
    target = _element_label(element)

    if name == "navigate":
        url = value.get("url")
        return f"navigate to {url}" if url else "navigate"
    if name == "done":
        return "done() - final verdict"
    if name in ("input", "type") and value.get("text") is not None:
        text = value["text"]
        return f'type "{text}" into "{target}"' if target else f'type "{text}"'
    if name == "click":
        return f'click "{target}"' if target else "click"
    if name == "send_keys" and value.get("keys"):
        return f'press "{value["keys"]}"'
    if name == "scroll":
        return "scroll page"
    if name == "wait":
        return "wait"
    if name == "extract_content":
        return "extract page content"

    label = _action_label(action)
    return label if len(label) <= 120 else label[:117] + "..."


def _match_haystack(action, element) -> str:
    """Text used to map an action to a scenario step (element + typed value)."""
    parts = [_element_label(element), _action_name(action)]
    value = _action_value(action)
    for key in ("text", "query", "keys"):
        if value.get(key):
            parts.append(str(value[key]))
    return " ".join(p for p in parts if p).lower()


def _scenario_step_for(action, element, table: dict, last_step):
    """Pick the scenario step an action belongs to, or the sticky previous one."""
    name = _action_name(action)
    if name == "done":
        return None
    if name == "navigate":
        return 1 if last_step is None else last_step

    haystack = _match_haystack(action, element)
    best = None  # (keyword length, step)
    for step, keywords in table.items():
        for keyword in keywords:
            keyword = keyword.lower().strip()
            if keyword and keyword in haystack:
                score = len(keyword)
                if best is None or score > best[0] or (score == best[0] and step < best[1]):
                    best = (score, step)
    return best[1] if best else last_step


def extract_steps(history, scenario_steps=None, hints=None) -> list[StepResult]:
    """Turn a browser-use AgentHistoryList into ONE StepResult per agent step.

    browser-use is inconsistent here: ``model_actions()`` returns a flat list of
    every action ever taken, while thoughts/urls/screenshots are one entry per
    step. Iterating the history entries directly keeps everything aligned to a
    real step, so each row in the report is one decision the agent made.

    Each action is labelled with the element it touched (not its numeric index)
    and attributed to a scenario step, so the report reads like the scenario.
    """
    entries = getattr(history, "history", None)
    if not entries:
        return []

    scenario_names = dict(scenario_steps or [])
    match_table = dict(_DEFAULT_STEP_MATCH)
    match_table.update(hints or {})

    steps: list[StepResult] = []
    last_step = None
    group_counts: dict = {}
    for entry in entries:
        metadata = getattr(entry, "metadata", None)
        if getattr(metadata, "step_number", None) == 0:
            # browser-use records the initial `navigate` as step 0. It is setup,
            # not an agent decision: counting it shifts every later step, so the
            # report's "agent step N" no longer matches the model's own numbering.
            continue

        state = getattr(entry, "state", None)
        output = getattr(entry, "model_output", None)
        results = getattr(entry, "result", None) or []
        elements = getattr(state, "interacted_element", None) or []

        action_models = list(getattr(output, "action", None) or []) if output is not None else []
        labels: list[str] = []
        step_number = last_step
        detail = ""
        if output is not None:
            mapped_numbers: list[int] = []
            for i, action in enumerate(action_models):
                element = elements[i] if i < len(elements) else None
                labels.append(_human_action(action, element))
                mapped = _scenario_step_for(action, element, match_table, last_step)
                if mapped is not None:
                    mapped_numbers.append(mapped)
            # `done` is the agent finishing, not a scenario step, so an entry that
            # only calls done() belongs under "Other actions".
            if mapped_numbers:
                step_number = mapped_numbers[-1]
            elif any(_action_name(a) == "done" for a in action_models):
                step_number = None
            current = getattr(output, "current_state", None)
            if current is not None:
                detail = (
                    getattr(current, "memory", "")
                    or getattr(current, "next_goal", "")
                    or getattr(current, "thinking", "")
                    or ""
                )

        if step_number is not None:
            last_step = step_number

        error = ""
        for result in results:
            found = getattr(result, "error", None)
            if found:
                error = str(found)
                break

        url = (getattr(state, "url", "") or "") if state is not None else ""

        display_index = ""
        if step_number is not None:
            group_counts[step_number] = group_counts.get(step_number, 0) + 1
            display_index = f"{step_number}.{group_counts[step_number]}"

        steps.append(
            StepResult(
                index=len(steps) + 1,
                name=" ; ".join(labels) if labels else "(no action recorded)",
                status=FAILED if error else PASSED,
                detail=detail,
                url=url,
                error=error,
                scenario_step=step_number,
                scenario_step_name=scenario_names.get(step_number, "") if step_number else "",
                display_index=display_index,
            )
        )
    return steps


def _is_login_page(url: str) -> bool:
    """SauceDemo serves the login form at the site root; any other path means we moved on."""
    if "saucedemo.com" not in (url or ""):
        return False
    try:
        path = urlparse(url).path
    except Exception:  # noqa: BLE001
        return False
    return path in ("", "/")


def detect_implausible_success(steps: list[StepResult], successful) -> Optional[str]:
    """Catch an agent that calls done(success=true) without doing the scenario.

    A small model can navigate once and then declare victory. If the claim of
    success rests on a browser that never left the login page, the run is not a
    pass no matter what the model said. Returns a reason string, or None.
    """
    if not successful or not steps:
        return None
    if _is_login_page(steps[-1].url):
        return (
            "the browser never left the login page, so no part of the scenario "
            "actually ran"
        )
    return None


# SauceDemo's order confirmation page. A verdict may only claim the order was
# placed if the browser actually ended up here.
_ORDER_COMPLETE_PATH = "/checkout-complete.html"

# Scenario-step wording that claims the order was placed (as opposed to merely
# continuing through checkout).
_COMPLETION_CLAIM = re.compile(
    r"complete\s+the\s+order|order\s+(?:was\s+)?(?:completed|placed)|finish(?:ed)?\s+the\s+order",
    re.IGNORECASE,
)


def detect_false_completion(verdict, steps: list[StepResult]) -> Optional[str]:
    """Catch a verdict that claims the order completed when it never did.

    Observed in a real run: the agent marked "6. Continue through checkout: PASS"
    and "7. Complete the order successfully: PASS" while the last recorded URL was
    still ``/checkout-step-one.html`` - the overview and finish pages were never
    reached, yet the summary said "the order ultimately completed successfully".
    A completion claim is only credible when the final page is the confirmation
    page. Returns a reason string, or None.
    """
    if verdict is None or not steps:
        return None
    claimed = [
        line
        for line in verdict.scenario_steps
        if _COMPLETION_CLAIM.search(line) and re.search(r"\bPASS(?:ED)?\b", line, re.IGNORECASE)
    ]
    if not claimed:
        return None
    final_url = steps[-1].url or ""
    if _ORDER_COMPLETE_PATH in final_url:
        return None
    try:
        path = urlparse(final_url).path or "/"
    except Exception:  # noqa: BLE001
        path = final_url or "(no page recorded)"
    return (
        f"the agent marked '{claimed[0].strip()}' as PASS, but the browser ended on "
        f"{path} and never reached the order confirmation page"
    )


def detect_contradictory_pass(verdict) -> Optional[str]:
    """Catch a PASS verdict whose own scenario steps are not all PASS.

    PASS means every scenario step passed. A step the agent marked FAIL or
    NOT RUN contradicts the verdict, so the run cannot be a pass.
    """
    if verdict is None or verdict.verdict != "PASS":
        return None
    line = first_not_passed_scenario_line(verdict)
    if not line:
        return None
    return f"the verdict was PASS, but the agent marked '{line}' as not passed"


def copy_screenshots(history, run_dir: Path, steps: list[StepResult], captured=None) -> None:
    """Copy screenshots into the run directory, one per step, as AFTER evidence.

    browser-use's own ``state.screenshot`` is captured at the START of a step, so
    ``step_09.png`` would show the page before step 9 ran (e.g. the empty checkout
    form for "type Test into First Name"), and the real result would appear on the
    next row. ``captured`` holds screenshots taken by the on_step_end hook, i.e.
    after the actions; when it is missing (older browser-use), fall back to
    shifting browser-use's paths by one step.
    """
    captured = list(captured or [])
    shots_dir = run_dir / "screenshots"

    if len(captured) >= len(steps) and any(captured):
        for i, step in enumerate(steps):
            data = captured[i]
            if not data:
                continue
            shots_dir.mkdir(parents=True, exist_ok=True)
            dest_name = f"step_{i + 1:02d}.png"
            try:
                (shots_dir / dest_name).write_bytes(data)
            except Exception:
                continue
            step.screenshot = f"screenshots/{dest_name}"
        return

    paths = _safe(lambda: history.screenshot_paths(), []) or []
    paths = [Path(p) if p else None for p in paths]
    if not any(paths):
        return
    # `extract_steps` drops browser-use's step-0 initial-navigation entry, so the
    # history path for step N is offset by however many entries were dropped.
    history_entries = _safe(lambda: len(history.history), 0) or 0
    skip = max(0, history_entries - len(steps))
    shots_dir.mkdir(parents=True, exist_ok=True)
    for i, step in enumerate(steps):
        # State after step N == state at the start of step N+1 (+ skipped entries).
        src = paths[i + 1 + skip] if i + 1 + skip < len(paths) else None
        if src is None or not src.exists():
            continue
        dest_name = f"step_{i + 1:02d}{src.suffix or '.png'}"
        dest = shots_dir / dest_name
        try:
            shutil.copy2(src, dest)
        except Exception:
            continue
        step.screenshot = f"screenshots/{dest_name}"


def recent_action(agent_output) -> str:
    """Best-effort, human-sized label for the actions chosen in one agent step."""
    try:
        actions = getattr(agent_output, "action", None) or []
    except Exception:
        return ""
    return " ; ".join(_action_label(a) for a in actions)


def build_step_callback(timings: dict):
    """Records per-step wall-clock time and the chosen action."""

    def _callback(browser_state, agent_output, step_number):
        now = time.time()
        elapsed = now - timings["last"]
        timings["last"] = now
        url = ""
        try:
            url = getattr(browser_state, "url", "") or ""
        except Exception:
            url = ""
        timings["steps"].append({
            "step": step_number,
            "action": recent_action(agent_output),
            "url": url,
            "seconds": round(elapsed, 1),
        })

    return _callback


def run_one(scenario_path: Path, cfg: dict, embed: bool, budget: int) -> int:
    from rich.console import Console

    console = Console()
    title, text = load_scenario(scenario_path)
    user = scenario_user(text)
    scenario_steps = parse_scenario_steps(text)
    hints = parse_report_hints(text)
    console.print(f"\n[bold cyan]Scenario:[/bold cyan] {title}  [dim]({scenario_path.name})[/dim]")

    from agent.agent_setup import AgentBuilder

    timings: dict = {
        "started": time.time(),
        "last": time.time(),
        "steps": [],
        "screenshots": [],
        "stopped_at_budget": False,
    }

    async def capture_after_step(hook_agent) -> None:
        """Screenshot AFTER the step's actions, for the report timeline.

        browser-use's own per-step screenshot is taken before the actions, so the
        evidence for "click Login" would still show the login page. This runs on
        every on_step_end, keeping the list aligned with `steps` (index 0 = step 1).
        """
        try:
            data = await hook_agent.browser_session.take_screenshot(full_page=False)
        except Exception:  # noqa: BLE001
            data = None
        timings["screenshots"].append(data)

    async def should_stop() -> bool:
        return budget > 0 and (time.time() - timings["started"]) > budget

    initial_actions = start_actions()

    builder = AgentBuilder(cfg, PROJECT_ROOT)
    agent, meta = builder.build_agent(
        build_task(title, text),
        initial_actions=initial_actions,
        step_callback=build_step_callback(timings),
        should_stop=should_stop,
        output_model=QAVerdict,
    )
    console.print(
        f"[dim]browser-use {meta.get('version')} - model {cfg.get('model')} - "
        f"max_steps {meta['max_steps']} - budget {budget}s[/dim]"
    )

    try:
        history = AgentBuilder.run_agent(agent, meta["max_steps"], on_step_end=capture_after_step)
    except KeyboardInterrupt:
        console.print("[bold red]Interrupted by user.[/bold red]")
        return 1
    except Exception as exc:
        console.print(f"[bold red]Agent run failed:[/bold red] {exc}")
        return 1

    elapsed_total = time.time() - timings["started"]
    timings["stopped_at_budget"] = budget > 0 and elapsed_total > budget

    final_result = _safe(lambda: history.final_result()) or ""
    done = bool(_safe(lambda: history.is_done(), False))
    steps = extract_steps(history, scenario_steps, hints) if history is not None else []

    # ---- the AI's own verdict ------------------------------------------------
    # browser-use validates final_result() against QAVerdict; parse_verdict is the
    # fallback for models that ignored the schema and answered in prose.
    verdict = _safe(lambda: history.structured_output) if history is not None else None
    if verdict is None:
        verdict = parse_verdict(final_result)

    # AI-authored findings only. Facts about the run itself (budget stop, false
    # success, unparsed output) go in `integrity`, never in the anomaly list.
    anomalies = anomalies_for_report(verdict, n_steps=len(steps)) if verdict else []
    integrity: list[dict] = []
    successful = (
        verdict.verdict == "PASS"
        if verdict is not None
        else bool(_safe(lambda: history.is_successful(), False))
    )

    # A verdict may claim things the browser never did. Code checks those claims
    # against the run's own trace; the model's prose is never taken on trust.
    false_success = detect_implausible_success(steps, successful)
    if false_success:
        integrity.append({
            "title": "Agent claimed success without running the scenario",
            "severity": "high",
            "description": (
                f"The model ended the run saying it succeeded, but {false_success}. "
                "This is not a genuine pass: nothing was actually exercised."
            ),
            "evidence": f"Final browser URL: {steps[-1].url or '(none recorded)'}",
            "impact": "The result is inconclusive; the run only proves the agent started the page.",
            "source": "harness",
        })

    false_completion = detect_false_completion(verdict, steps)
    if false_completion:
        integrity.append({
            "title": "Agent claimed steps that never ran",
            "severity": "high",
            "description": (
                f"The model's verdict claims the scenario finished, but {false_completion}. "
                "The later scenario steps are therefore unverified - treat them as NOT RUN."
            ),
            "evidence": f"Final browser URL: {steps[-1].url or '(none recorded)'}",
            "impact": "The run cannot be reported as complete; only the steps up to the last real page are evidence.",
            "source": "harness",
        })

    contradictory_pass = detect_contradictory_pass(verdict)
    if contradictory_pass:
        integrity.append({
            "title": "Verdict contradicts its own scenario steps",
            "severity": "high",
            "description": (
                f"The model returned PASS, but {contradictory_pass}. "
                "A verdict that disagrees with its own per-step assessment is not a pass."
            ),
            "evidence": "See the scenario steps listed for this test.",
            "impact": "The result is not trustworthy as a pass; review the timeline.",
            "source": "harness",
        })

    if not done:
        if timings["stopped_at_budget"]:
            stop_reason = f"the {budget}s wall-clock budget was reached"
        else:
            stop_reason = f"the agent used all {meta['max_steps']} allowed steps"
        integrity.append({
            "title": "Run stopped before the agent reached a final verdict",
            "severity": "medium",
            "description": (
                f"The agent stopped after {elapsed_total:.0f}s because {stop_reason}, without "
                "calling done(), so the scenario is incomplete. The steps that did run are "
                "still valid evidence."
            ),
            "evidence": "Step timings below.",
            "impact": "The end-to-end result is not conclusive; raise Max Steps or the demo budget.",
            "source": "harness",
        })
    elif verdict is None:
        integrity.append(unparsed_card(final_result))

    # Attach per-step timing to the matching step so it shows up in the report.
    # Timing callbacks are numbered like the agent's "Step N:" log, and history
    # entry N is that same step, so map by number instead of positionally.
    timing_by_step = {t["step"]: t for t in timings["steps"]}
    for i, step in enumerate(steps):
        timing = timing_by_step.get(i + 1)  # callback numbers steps from 1
        if not timing:
            continue
        note = f"step time: {timing['seconds']}s"
        step.detail = f"{step.detail}  ({note})" if step.detail else f"({note})"

    # ---- outcome: binary on purpose -----------------------------------------
    # PASS only when the agent says PASS and every check below agrees. Anything
    # else - an unfinished run, an unparsed verdict, a contradiction, an anomaly
    # - is a FAIL. There is no third "mixed" state.
    if false_success or false_completion or contradictory_pass:
        outcome = FAILED
    elif not done or verdict is None:
        outcome = FAILED
    else:
        outcome = PASSED if verdict.verdict == "PASS" else FAILED
        if outcome == PASSED and anomalies:
            outcome = FAILED          # a PASS with findings is a contradiction

    if verdict is not None:
        assertion = verdict.summary
    elif done:
        assertion = final_result or "the agent finished without a structured verdict"
    else:
        assertion = "the agent did not reach a final verdict (run stopped early)"
    if false_success:
        assertion = f"Agent reported success, but {false_success}."
    elif false_completion:
        assertion = f"Agent claimed steps that never ran: {false_completion}."
    elif contradictory_pass:
        assertion = f"The agent's PASS verdict contradicts its own scenario steps: {contradictory_pass}."

    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = PROJECT_ROOT / "reports" / "agent" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if history is not None:
        copy_screenshots(history, run_dir, steps, captured=timings.get("screenshots"))

    # A step's own status only records whether its browser action raised an error,
    # so the step where the scenario actually failed is still marked PASSED. The
    # model says which agent step it saw the defect on; anchor on the first scenario
    # step it marked FAIL, and only trust numbers that exist in the run.
    failed_step_index = None
    if outcome != PASSED and verdict is not None:
        failed_step_index = failed_step_for_verdict(verdict, len(steps))
    if outcome == FAILED and steps and failed_step_index is None:
        failed_step_index = steps[-1].index
    if failed_step_index is not None:
        # The row is badged FAILED, so its text must describe the failure - never
        # the model's overall summary (which can say "the order completed").
        failed_scenario_line = (
            first_failed_scenario_line(verdict) if verdict is not None else None
        )
        step_assertion = (
            f"Scenario step did not pass: {failed_scenario_line}"
            if failed_scenario_line
            else assertion
        )
        for step in steps:
            if step.index == failed_step_index:
                step.status = FAILED
                if not step.error:
                    step.error = step_assertion
                break

    test = TestResult(
        name=f"ai-agent :: {title}",
        outcome=outcome,
        duration=elapsed_total,
        user=user,
        steps=steps,
        assertion=assertion,
        traceback=final_result,
        note=("stopped at the time budget" if timings["stopped_at_budget"] else ""),
        failed_step_index=failed_step_index,
        scenario_steps=list(verdict.scenario_steps) if verdict else [],
    )

    report = RunReport(
        run_id=run_id,
        mode="ai-agent (Browser Use)",
        framework="browser-use",
        tests=[test],
        scenarios=[scenario_path.name],
        model=cfg.get("model", ""),
        provider=cfg.get("provider", ""),
        anomalies=anomalies,
        integrity=integrity,
    )
    if verdict is not None:
        summary = verdict.summary
    elif done and final_result:
        summary = final_result
    else:
        last_observation = steps[-1].detail.strip() if steps and steps[-1].detail else ""
        summary = "The agent stopped before reaching a final verdict."
        if last_observation:
            summary += f" Last observation: {last_observation[:400]}"
    if false_completion or contradictory_pass:
        # Never present a claim the trace already disproved as if it were fact.
        summary = (
            "The agent's account below is contradicted by the browser trace "
            f"({false_completion or contradictory_pass}). See Run Integrity.\n\n{summary}"
        )
    report.summary = (
        f"{summary}\n\nTotal wall-clock time: {elapsed_total:.0f}s across {len(steps)} agent steps. "
        f"Verdict: {report.verdict}."
    )
    report.what_it_did = ""
    report.automated_vs_human = ""

    md_path = write_markdown(report, run_dir)
    html_path = write_html(report, run_dir, embed=embed, title="SauceDemo AI QA Report")

    if timings["steps"]:
        console.print("\n[bold]Step timings[/bold]")
        for t in timings["steps"]:
            label = t["action"] if len(t["action"]) <= 100 else t["action"][:97] + "..."
            console.print(f"  step {t['step']:>2}  {t['seconds']:>6.1f}s  {label}")
    console.print(f"\n[bold green]Verdict:[/bold green] {report.verdict}")
    console.print(f"[bold green]Report:[/bold green] {md_path}")
    console.print(f"[bold green]HTML:[/bold green]   {html_path}")
    return 0 if not report.failed else 1


def generate_and_run_tests(scenario_files, run_tests: bool) -> int:
    """Freeze each scenario into a deterministic pytest file, then optionally run it.

    Generation is idempotent (see tools/generate_tests.py): it only calls the model
    when the scenario's text changed or no test exists yet, so repeating an agent
    run does not re-generate an unchanged test.

    Returns a process-style exit code: 0 when everything succeeded.
    """
    from tools.generate_tests import generate, test_path_for

    rc = 0
    generated: list[Path] = []
    print("\nDeterministic tests")
    for scenario_path in scenario_files:
        path = scenario_path.resolve()
        gen_rc = generate(scenario_path=path, force=False, dry_run=False)
        if gen_rc != 0:
            rc = max(rc, gen_rc)
            continue
        test_file = test_path_for(path)
        if test_file.exists():
            generated.append(test_file)

    if not run_tests or not generated:
        return rc

    import subprocess

    cmd = [sys.executable, "-m", "pytest", *[str(f) for f in generated], "-q"]
    print(f"\nRunning deterministic tests: {' '.join(cmd[2:])}")
    sys.stdout.flush()  # keep our log lines ahead of the child process output
    pytest_rc = subprocess.call(cmd, cwd=str(PROJECT_ROOT))
    print(f"Deterministic test exit code: {pytest_rc}")
    return max(rc, 0 if pytest_rc == 0 else 1)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a SauceDemo scenario with the Browser Use AI agent")
    parser.add_argument("--scenario", help="path to a scenario file, e.g. scenarios/checkout_problem_user.md")
    parser.add_argument("--all", action="store_true", help="run every scenario in scenarios/")
    parser.add_argument("--embed", dest="embed", action="store_true", default=True,
                        help="embed screenshots in index.html as base64 (default)")
    parser.add_argument("--no-embed", dest="embed", action="store_false",
                        help="keep screenshots as files next to index.html")
    parser.add_argument("--budget", type=int, default=None,
                        help="hard wall-clock cap in seconds for the agent (default: MODEL_ACCESS.md)")
    parser.add_argument("--max-steps", type=int, default=None,
                        help="override the maximum number of agent steps")
    parser.add_argument("--headed", dest="headless", action="store_false", default=None,
                        help="show the browser window (slower, nicer for a demo)")
    parser.add_argument("--headless", dest="headless", action="store_true", default=None,
                        help="run without a visible browser window")
    parser.add_argument("--generate-test", dest="generate_test", action="store_true",
                        help="after the run, generate/refresh the deterministic pytest file for each scenario "
                             "(only when the scenario changed - see tools/generate_tests.py)")
    parser.add_argument("--run-test", dest="run_test", action="store_true",
                        help="after generating, run the generated deterministic pytest file(s) "
                             "(implies --generate-test)")
    args = parser.parse_args()

    if not args.scenario and not args.all:
        parser.error("provide --scenario <file> or --all")

    cfg_loader = ModelAccessConfig(PROJECT_ROOT / "MODEL_ACCESS.md")
    cfg = cfg_loader.to_dict()
    cfg["api_key"] = cfg_loader.resolve_api_key()
    if not cfg["api_key"]:
        print("No API key found. Set the API Key in MODEL_ACCESS.md or the matching "
              "environment variable (e.g. OPENROUTER_API_KEY / OPENAI_API_KEY).")
        return 2

    # CLI overrides for the speed / reliability profile. They have to be applied to
    # the loader's dict as well, otherwise apply_to_env() would export stale values.
    if args.max_steps is not None:
        cfg["max_steps"] = args.max_steps
    if args.headless is not None:
        cfg["headless"] = args.headless
    cfg_loader.config.update(cfg)
    cfg_loader.apply_to_env()

    budget = cfg.get("demo_budget", 420) if args.budget is None else args.budget

    if args.all:
        scenario_files = sorted(SCENARIOS_DIR.glob(SCENARIO_GLOB))
    else:
        scenario_files = [Path(args.scenario)]

    if not scenario_files:
        print(f"No scenario files found in {SCENARIOS_DIR}")
        return 2

    worst = 0
    for path in scenario_files:
        if not path.exists():
            print(f"Scenario not found: {path}")
            return 2
        worst = max(worst, run_one(path, cfg, args.embed, budget))

    if args.generate_test or args.run_test:
        worst = max(worst, generate_and_run_tests(scenario_files, args.run_test))
    return worst


if __name__ == "__main__":
    sys.exit(main())
