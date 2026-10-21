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
    parse_verdict,
    unparsed_card,
)
from config.load_model_access import ModelAccessConfig  # noqa: E402
from reporting import (  # noqa: E402
    FAILED,
    MIXED,
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


def extract_steps(history) -> list[StepResult]:
    """Turn a browser-use AgentHistoryList into ONE StepResult per agent step.

    browser-use is inconsistent here: ``model_actions()`` returns a flat list of
    every action ever taken, while thoughts/urls/screenshots are one entry per
    step. Iterating the history entries directly keeps everything aligned to a
    real step, so each row in the report is one decision the agent made.
    """
    entries = getattr(history, "history", None)
    if not entries:
        return []

    steps: list[StepResult] = []
    for entry in entries:
        state = getattr(entry, "state", None)
        output = getattr(entry, "model_output", None)
        results = getattr(entry, "result", None) or []

        actions: list[str] = []
        detail = ""
        if output is not None:
            actions = [_action_label(a) for a in (getattr(output, "action", None) or [])]
            current = getattr(output, "current_state", None)
            if current is not None:
                detail = (
                    getattr(current, "memory", "")
                    or getattr(current, "next_goal", "")
                    or getattr(current, "thinking", "")
                    or ""
                )

        error = ""
        for result in results:
            found = getattr(result, "error", None)
            if found:
                error = str(found)
                break

        url = (getattr(state, "url", "") or "") if state is not None else ""

        steps.append(
            StepResult(
                index=len(steps) + 1,
                name=" ; ".join(actions) if actions else "(no action recorded)",
                status=FAILED if error else PASSED,
                detail=detail,
                url=url,
                error=error,
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


def copy_screenshots(history, run_dir: Path, steps: list[StepResult]) -> None:
    """Copy browser-use screenshots into the run directory, one per step.

    Attaches by index (step N -> screenshots/step_NN.png) so a step with no
    screenshot cannot shift the images of the later steps.
    """
    paths = _safe(lambda: history.screenshot_paths(), []) or []
    paths = [Path(p) if p else None for p in paths]
    if not any(paths):
        return
    shots_dir = run_dir / "screenshots"
    shots_dir.mkdir(parents=True, exist_ok=True)
    for i, src in enumerate(paths):
        if src is None or not src.exists():
            continue
        dest_name = f"step_{i + 1:02d}{src.suffix or '.png'}"
        dest = shots_dir / dest_name
        try:
            shutil.copy2(src, dest)
        except Exception:
            continue
        if i < len(steps):
            steps[i].screenshot = f"screenshots/{dest_name}"


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
    console.print(f"\n[bold cyan]Scenario:[/bold cyan] {title}  [dim]({scenario_path.name})[/dim]")

    from agent.agent_setup import AgentBuilder

    timings: dict = {"started": time.time(), "last": time.time(), "steps": [], "stopped_at_budget": False}

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
        history = AgentBuilder.run_agent(agent, meta["max_steps"])
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
    steps = extract_steps(history) if history is not None else []

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

    # A model that declares victory on the login page must not be able to PASS the run.
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
        timing = timing_by_step.get(i)
        if not timing:
            continue
        note = f"step time: {timing['seconds']}s"
        step.detail = f"{step.detail}  _({note})_" if step.detail else f"_({note})_"

    # ---- outcome: the model decides, code only downgrades --------------------
    if false_success:
        outcome = FAILED
    elif not done:
        outcome = MIXED if steps else FAILED
    elif verdict is None:
        # No usable verdict: never a pass. The raw narrative stays in the timeline.
        outcome = MIXED
    else:
        outcome = {"PASS": PASSED, "MIXED": MIXED, "FAIL": FAILED}[verdict.verdict]
        if outcome == PASSED and anomalies:
            outcome = MIXED          # the model contradicted itself

    if verdict is not None:
        assertion = verdict.summary
    elif done:
        assertion = final_result or "the agent finished without a structured verdict"
    else:
        assertion = "the agent did not reach a final verdict (run stopped early)"
    if false_success:
        assertion = f"Agent reported success, but {false_success}."

    run_id = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    run_dir = PROJECT_ROOT / "reports" / "agent" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    if history is not None:
        copy_screenshots(history, run_dir, steps)

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
        for step in steps:
            if step.index == failed_step_index:
                step.status = FAILED
                if not step.error:
                    step.error = assertion
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
