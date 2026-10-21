# SauceDemo AI QA — agent that runs the scenario, then freezes it into a deterministic test

One agent, two jobs:

1. **Analyze & execute** a scenario adaptively (Browser Use + LLM), flag anomalies, produce an evidence-rich report.
2. **Freeze** that scenario into a deterministic Playwright/pytest file (`tools/generate_tests.py`), generated **once** and re-run with no model at all.

| Piece | What it does | Needs a model? |
|---|---|---|
| **AI agent** (`agent/`) | Runs the scenario end-to-end, observes the live UI, authors the findings | yes (`MODEL_ACCESS.md`) |
| **Test generator** (`tools/generate_tests.py`) | Turns a `scenarios/*.md` into a pytest file (hash-gated: only when the scenario changes) | yes (one call) |
| **Deterministic gate** (`tests/generated/`) | The generated pytest files — the gate of record | **no** |

All three share **one scenario source** (`scenarios/*.md`) and **one reporter** (`reporting/`), so the report format is identical whichever path you run.

## Quick start

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # deterministic gate (pytest + Playwright)
python -m playwright install chromium    # one-off browser download

pip install -r requirements-agent.txt    # optional: adds browser-use for the AI path
```

If the model or the API is unavailable, the generated pytest suite still runs — that is the point of freezing it.

## Run the agent (analyze + execute)

```powershell
.venv\Scripts\python.exe agent\run_scenario.py --scenario scenarios\checkout_problem_user.md
.venv\Scripts\python.exe agent\run_scenario.py --all          # every scenario in scenarios\
```
- Report: `reports\agent\<timestamp>\` (`report.md`, `index.html`).

The **agent performs the login itself** (scenario step 1), like a real user.

### Speed / demo options

```powershell
--headless        # no visible browser window (faster)
--budget 600      # hard wall-clock cap in seconds; the run stops and still reports
--max-steps 20    # cap on agent steps
```

What keeps it fast:

- **`MODEL_ACCESS.md` → Speed / Reliability Profile.** Judge, planning and "thinking" are off by default, flash mode is on, timeouts/retries capped. Each of these used to add an extra model call (or minutes) per step.
- **A wall-clock budget** stops a wedged run instead of hanging forever, and any steps that did run are still reported as evidence.

### Run the agent **and** generate + run the deterministic test

```powershell
.venv\Scripts\python.exe agent\run_scenario.py --scenario scenarios\checkout_problem_user.md --generate-test --run-test
```

| Flag | Effect |
|---|---|
| `--generate-test` | After the agent run, generate/refresh `tests\generated\test_<scenario>.py` — **only if the scenario text changed** (hash-checked, so repeat runs don't call the model) |
| `--run-test` | Also run the generated file with pytest (implies `--generate-test`) |

One command, three results: the agent's report, the frozen test file, and the deterministic verdict.

## Generate tests only

```powershell
.venv\Scripts\python.exe tools\generate_tests.py --scenario scenarios\checkout_standard_user.md
.venv\Scripts\python.exe tools\generate_tests.py --all                # every scenario
.venv\Scripts\python.exe tools\generate_tests.py --scenario scenarios\checkout_standard_user.md --force      # ignore the hash
.venv\Scripts\python.exe tools\generate_tests.py --scenario scenarios\checkout_standard_user.md --dry-run    # print, write nothing
.venv\Scripts\python.exe tools\generate_tests.py --all --check       # CI: exit 1 if any scenario drifted
```

Generation is keyed by a **SHA-256 of the scenario's normalised text** in `tests\generated\manifest.json`:

- unchanged hash + file exists → **skips, no LLM call** ("up to date")
- hash changed (or `--force`) → regenerates
- `--check` exits `1` when a scenario has drifted, so CI can block on it

The generated file only imports the shared `tests\steps\step_helpers.py`, so the selectors live in your hand-owned helper module, not in LLM output.

## Run the deterministic gate

```powershell
.venv\Scripts\python.exe -m pytest -v                 # headless, report embedded
.venv\Scripts\python.exe -m pytest -v --headed        # watch the browser
.venv\Scripts\python.exe -m pytest -v --no-embed      # small HTML, images as files
.venv\Scripts\python.exe -m pytest tests\generated\test_checkout_problem_user.py -v   # one scenario
```
- `test_checkout_standard_user.py` — full scenario happy path (**passes**).
- `test_checkout_problem_user.py` — same scenario for `problem_user` (**fails by design**: checkout is broken for that account, so the failure is the finding).
- Expected result with both generated: **1 failed, 1 passed** (exit code 1).
- Report: `reports\pytest\<timestamp>\` (`report.md`, `index.html`).

## The scenario (single source of truth)

`scenarios/checkout_standard_user.md` and `scenarios/checkout_problem_user.md` hold the user account, the test data, the 7 steps and the expected results:

1. Log into the application
2. Add **Sauce Labs Backpack** and **Sauce Labs Bike Light** to the cart
3. Open the shopping cart
4. Start checkout
5. Enter valid customer information (Test / User / 12345)
6. Continue through checkout
7. Complete the order successfully

`prompts/qa_system_prompt.md` contains **QA rules only** (verify field values, flag anomalies, report with evidence) — no step list — so the instructions cannot drift away from the scenario. Lines in a scenario starting with `>` are human-only notes, stripped by `build_task()` before the agent sees them.

## The reports

### Who writes the findings

The agent returns a structured verdict (`agent/qa_verdict.py`) when it calls
`done()`: the verdict, a summary, one entry per scenario step, and a list of
anomalies with severity, scenario step, expected vs observed, evidence and impact.
Those anomaly cards are the agent's own words; code only validates the structure
and links the evidence.

Code-authored facts about the run itself — budget stop, claimed success without
leaving the login page, unparsed verdict — are reported separately under
**Run Integrity**, never mixed into the AI findings.

The deterministic pytest suite does not use this contract; it stays model-free.

Markdown (`report.md`) follows this format:

`Executive Summary` → `Anomalies Found` (if any) → `Run Integrity` (if any) → `Failures Found` → `Step-by-Step Timeline` → `Evidence`.

`Anomalies Found` holds the findings (authored by the agent in the AI path, derived
from the failing test in the pytest path); `Run Integrity` holds code-authored facts
about the run (budget stop, false success, unparsed verdict). `Failures Found` always
follows, so a failing run stays visible even when no anomaly was authored.

`index.html` shows **one row per scenario step** with, in the same place:

- the step number and name,
- its own **PASSED / FAILED / NOT RUN** badge,
- the observation and (for a failure) the assertion message,
- the **screenshot taken right after that step** (the failure row is highlighted and enlarged), plus the traceback in a collapsible block.

Screenshots are **embedded as base64 by default**, so `index.html` is a single self-contained file you can move, email or attach to a bug report. Use `--no-embed` to keep it small with image files next to it instead. A screenshot is captured after **every** step and again at the moment a test fails.

## Project structure

```
saucedemo-ai-qa-agent/
├─ MODEL_ACCESS.md              # model/provider/API key for the AI path + generator
├─ requirements.txt             # deterministic gate (pytest + Playwright)
├─ requirements-agent.txt       # AI path (browser-use, rich)
├─ pytest.ini                   # testpaths = tests/generated
├─ scenarios/                   # <- the scenario, written once
│   ├─ checkout_standard_user.md
│   └─ checkout_problem_user.md
├─ prompts/
│   └─ qa_system_prompt.md      # QA rules only (no steps)
├─ config/
│   └─ load_model_access.py     # parses MODEL_ACCESS.md -> config/env
├─ agent/                       # AI path (analyze + execute)
│   ├─ run_scenario.py          # CLI: run a scenario with the agent
│   ├─ qa_verdict.py            # the AI output contract (schema + parser + mapper)
│   └─ agent_setup.py           # builds the Browser Use agent
├─ tools/
│   └─ generate_tests.py        # scenario .md -> deterministic pytest file (hash-gated)
├─ tests/                       # deterministic gate (no model)
│   ├─ conftest.py              # page fixture + per-step screenshots + report
│   ├─ steps/step_helpers.py    # the shared scenario steps (generator vocabulary)
│   └─ generated/               # AUTO-GENERATED tests + manifest.json
├─ reporting/                   # ONE reporter used by BOTH paths
│   ├─ model.py                 # run/step/test result objects
│   ├─ markdown.py              # report.md
│   └─ html.py                  # index.html (per-step result + embedded screenshot)
└─ reports/
    ├─ agent/<timestamp>/       # AI-path output
    └─ pytest/<timestamp>/      # deterministic output
```

## Configure the model

Edit `MODEL_ACCESS.md` (provider, model, base URL, API key). Any provider or any OpenAI-compatible endpoint works. You can also leave the API key blank and set the matching environment variable.

The active config is **Google Gemini's free tier** (`Provider: gemini` → `ChatGoogle`, model `gemini-3.5-flash-lite`). Notes:

- Use the **native** Gemini API, not the OpenAI-compatible endpoint — that endpoint rejects browser-use's hardcoded `frequency_penalty` (`400 Unknown name "frequency_penalty"`).
- Keep `Add Schema To Prompt: false`; setting it `true` silently reroutes to `ChatOpenAI`, which hits the same 400.
- Free-tier rate limits: `-flash` models are 5 requests/min; `flash-lite` is 15 requests/min.

**Ollama** (offline, no API key) and **OpenRouter** are documented as commented-out alternatives in `MODEL_ACCESS.md`. Prefer a vision-capable model so the agent can spot visual anomalies, and keep `Temperature` low for reproducibility.

## Notes

- The AI agent explores, explains and **authors the anomaly list**; code only validates it and enforces run integrity. The generated pytest suite is still the **gate of record**. When they disagree, the agent's trace is the evidence attached to the failing test.
- **Review the first generation** of any test file — the LLM can add redundant or slightly-off assertions.
- `problem_user` deliberately breaks checkout (typing Last Name corrupts First Name), which is why its test fails — that failure is the point of the scenario.
