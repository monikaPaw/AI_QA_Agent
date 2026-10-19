# AI-Driven QA Agent – Solution Architecture

This document describes the end-to-end architecture of the AI-driven QA agent for SauceDemo. It is designed to be exported and referenced alongside an Excalidraw diagram (`aq-agent-architecture.excalidraw`) for visualization in Excalidraw/Miro.

## 1. Purpose & Goals

The solution provides two complementary paths for running the same SauceDemo checkout scenario with a single source of truth:

- **AI Agent Path** (`agent/`) – Adaptive, browser-use powered execution that observes live UI, detects anomalies, and produces evidence-rich reports.
- **Deterministic Regression Path** (`tests/`) – Playwright/pytest implementation serving as the **gate of record**.
- **Shared Artifacts** – Common scenario definitions (`scenarios/*.md`), QA rules (`prompts/qa_system_prompt.md`), and unified reporter (`reporting/`) producing identical report formats.

**Key Goals:**
- Single source of truth for scenarios (prevent drift between AI and deterministic paths).
- Fast, resilient runs with wall-clock budgets, retries, and headless execution.
- Evidence-first reporting (per-step screenshots, timelines, anomalies).
- Model-agnostic (Gemini native API or any OpenAI-compatible endpoint; works without Ollama).
- Keep deterministic suite runnable without LLM dependencies.

## 2. High-Level Architecture

```text
┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                              User / CI                                                    │
│                                     (CLI: pytest / agent/run_scenario.py)                                   │
└─────────────────────────────────────────────────┬─────────────────────────────────────────────────────────┘
                                                  │
                                                  ▼
┌─────────────────────────────────────────────────────────────────────────────────────────────────────────┐
│                                          Scenario Source (Single Truth)                                    │
│                        scenarios/checkout_*.md  ── shared by AI path & deterministic gate                   │
└─────────────────────────────────────────────────┬─────────────────────────────────────────────────────────┘
                                                  │
                                          ┌───────▼───────┐
                                          │   QA Rules    │
                                          │ prompts/*.md  │
                                          └───────────────┘
                                                  │
                                    ┌─────────────┴─────────────┐
                                    │                           │
                                    ▼                           ▼
                          ┌─────────────────────────┐   ┌─────────────────────────────────┐
                          │      AI Path (Agent)    │   │    Deterministic Path (Gate)    │
                          │  agent/run_scenario.py  │   │     tests/generated/*.py        │
                          │  agent/agent_setup.py   │   │     tests/steps/*.py            │
                          │  tools/generate_tests.py│   │     pytest + Playwright         │
                          └───────────┬─────────────┘   └─────────────┬───────────────────┘
                                      │                           │
                                      └─────────────┬─────────────┘
                                                    │ (shared)
                                                    ▼
                                        ┌─────────────────────────────────────────┐
                                        │           Shared Reporter                │
                                        │  reporting/model.py | markdown.py |     │
                                        │  html.py (per-step + embedded screenshots)│
                                        └─────────────┬───────────────────────────┘
                                                      │
                                                      ▼
                                                ┌─────────────────────────┐
                                                │      Output Artifacts    │
                                                │  reports/agent/<ts>/    │
                                                │  reports/pytest/<ts>/   │
                                                │  (report.md, index.html) │
                                                └─────────────────────────┘
```

## 3. Core Components

### 3.1 Scenario Layer (`scenarios/`)
- **Format**: Markdown files containing user account, password, test data, numbered steps (1–7), and expected results.
- **Examples**: `checkout_standard_user.md`, `checkout_problem_user.md`.
- **Notes**: Lines starting with `>` are treated as human notes and excluded from agent task to avoid leaking expected outcomes.
- **Purpose**: Enforces "write once, run via both paths" contract.

### 3.2 AI Path (`agent/`)
| File | Responsibility |
|---|---|
| `run_scenario.py` | CLI orchestration: load scenario, parse args (`--budget`, `--max-steps`, `--headless`, `--generate-test`, `--run-test`, `--all`), build task, invoke agent, collect history, transform to `RunReport`, write outputs. Includes wall-clock budget and always produces a report. After the run, optionally generates and runs the deterministic test. |
| `agent_setup.py` | Builds Browser Use agent with version-tolerant imports. Creates LLM via provider mapping (OpenRouter/OpenAI/Groq/Cerebras/Anthropic/Google/Custom/Ollama). Handles browser session/profile construction, timing knobs, and persistent profile reuse. |

**Key Behaviors:**
- **Per-step alignment**: History entries mapped 1:1 to agent steps (actions, thoughts, URLs, results, screenshots) for report fidelity.
- **Failure guardrails**: If UI issue persists after ~2 attempts (e.g., field won’t persist), agent instructed to stop workarounds, record evidence, finish with `done(success=false, ...)`.

### 3.3 Deterministic Regression Path (`tests/`)
| Component | Responsibility |
|---|---|
| `tests/generated/*.py` | **Auto-generated** scenario tests (e.g. `test_standard_user_checkout.py`, `test_problem_user_checkout.py`). Produced by `tools/generate_tests.py`, not written by hand. |
| `tests/generated/manifest.json` | Hash-gate: scenario path → SHA-256 + generated file + model. Generation is skipped unless the scenario changed. |
| `tests/steps/step_helpers.py` | Shared step implementations mirroring the 7-step scenario (login, add items, cart, checkout, fill info, continue, complete). Serves as the generator's vocabulary. |
| `tests/conftest.py` | Pytest fixtures: browser/page setup, per-step screenshot capture, failure screenshot on error, hooks to build unified report artifacts (`report.md`, `index.html`) under `reports/pytest/<ts>/`. |
| `pytest.ini` | Pytest configuration (`testpaths = tests/generated`, defaults). |

### 3.3.1 Test Generator (`tools/generate_tests.py`)

Turns a scenario `.md` into a deterministic pytest file. The LLM is used **only at authoring time** — normal runs execute the generated file with no model involved.

- **Hash-gated**: keyed by SHA-256 of the scenario's normalised text in `manifest.json`. Unchanged hash + existing file → skip (no LLM call). Changed (or `--force`) → regenerate.
- **Grounded**: the prompt injects the scenario, the `step_helpers.py` source, and a style example, so selectors come from the hand-owned helper module rather than being invented.
- **Validated**: generated code is `ast.parse`d and must define the expected `test_*` function, otherwise it is rejected and the raw output saved for inspection.
- **CLI**: `--scenario`, `--all`, `--force`, `--dry-run`, `--check` (CI drift gate, exits 1 when a scenario changed but its test did not).

**Note:** `problem_user` is known-broken by design; its test is expected to fail — the failure is the finding.

### 3.4 Shared Reporter (`reporting/`)
Unified model + renderers used by both paths.

| File | Purpose |
|---|---|
| `model.py` | Data classes: `StepResult`, `TestResult`, `RunReport`. Statuses: `PASSED`, `FAILED`, `NOT_RUN`, `MIXED`. |
| `markdown.py` | Renders `report.md` with sections: Executive Summary → Failures Found → Step-by-Step Timeline → Evidence → Suggested Improvements → Evolution Ideas. |
| `html.py` | Renders `index.html` (single-file by default). Shows **one row per scenario step** with status badge, observation/assertion, **screenshot taken right after that step**, and collapsible traceback on failure. Screenshots embedded as base64 by default (`--no-embed` writes image files). |

**Artifacts:**
- `reports/agent/<YYYY-MM-DD_HH-MM-SS>/` – AI path outputs
- `reports/pytest/<YYYY-MM-DD_HH-MM-SS>/` – Regression outputs

### 3.5 Configuration & Prompts
| Component | Purpose |
|---|---|
| `config/load_model_access.py` | Parses `MODEL_ACCESS.md` → resolved config (provider, model, base_url, api_key/env var resolution). Supports OpenAI-compatible endpoints. |
| `prompts/qa_system_prompt.md` | **Rules-only** QA prompt (observe, verify effects, detect anomalies, evidence-driven). Contains no step list/user data, so paths cannot drift. Defines anomaly categories and reporting expectations (verdict: PASS/FAIL/MIXED). |
| `MODEL_ACCESS.md` | User-editable provider/model/API settings. Speed profile guidance (judge/planning/thinking off, flash mode, timeouts/caps). |

## 4. Data Flow

### 4.1 AI Path Execution
1. **Input**: CLI args + `scenarios/*.md` + `MODEL_ACCESS.md` + `qa_system_prompt.md`.
2. **Config Load**: `ModelAccessConfig` resolves provider/model/base_url/key.
3. **Agent Build**: `agent_setup.py` imports Browser Use, constructs LLM + BrowserSession, applies timing/budget knobs.
4. **Task Build**: `run_scenario.py` filters human-only `>` notes, appends QA rules/guardrails.
5. **Execution**: Browser Use agent runs against `https://www.saucedemo.com/`, performing the login itself (scenario step 1). Captures actions, thoughts, URLs, results, screenshots per step.
6. **Normalization**: History → `list[StepResult]`, build `TestResult(s)`, `RunReport` (includes budget/metadata). Anomalies are inferred heuristically from the agent's own trace; Suggested Improvements / Evolution Ideas are derived from the run.
7. **Render & Write**: `write_markdown` + `write_html` to `reports/agent/<ts>/`.
8. **Optional (with `--generate-test` / `--run-test`)**: `tools/generate_tests.py` generates (if the scenario hash changed) then runs the deterministic pytest file.

### 4.2 Deterministic Path Execution
1. **Input**: Pytest discovers `tests/generated/*.py` (generated from `scenarios/*.md`).
2. **Setup**: `conftest.py` provisions browser/page (Chromium).
3. **Step Execution**: `step_helpers.py` runs 7 steps with explicit assertions; conftest captures screenshot after each step and on failure.
4. **Report Build**: Collected results transformed into unified `RunReport` structure.
5. **Render & Write**: Same reporters write to `reports/pytest/<ts>/`.

## 5. Technology Stack

| Layer | Technology | Notes |
|---|---|---|
| Runtime | Python 3.x | Project uses `venv` on Windows. |
| AI Agent | [browser-use](https://github.com/browser-use/browser-use) | Version-tolerant (0.13.x target). Async `Agent.run()`. |
| LLM Access | Gemini native API / OpenAI-compatible APIs | Provider mapping supports Google, OpenAI, Groq/Cerebras/Anthropic/Ollama. Vision-capable models recommended. |
| Browser Automation (AI) | Browser Use + Chromium | Persistent profile via `user_data_dir`, executable pinning, `highlight_elements=False` to avoid timeouts. |
| Browser Automation (Det.) | [Playwright](https://playwright.dev/python/) | Used by the pytest gate. `playwright install chromium` required. |
| Testing | [pytest](https://docs.pytest.org/) | Regression gate, exit code reflects pass/fail (problem_user expected fail). |
| Reporting | Jinja2/stdlib HTML + Markdown | Single-file HTML with base64 images by default. |
| Config | Plain Markdown (`MODEL_ACCESS.md`) | Human-editable; parsed at runtime. |

## 6. Integrations & Interfaces

- **Target App**: SauceDemo (`https://www.saucedemo.com/`, `inventory.html`, cart/checkout flows)
- **LLM Providers**: Google Gemini (active in `MODEL_ACCESS.md`), OpenAI, Groq, Cerebras, Anthropic, custom OpenAI-compatible, Ollama, OpenRouter (commented out).
- **CLI**: Agent CLI (`agent/run_scenario.py`) and pytest CLI.
- **Artifacts**: Filesystem under `reports/` and `.browser_profile/` (git-ignored). No external DB by default.

## 7. Non-Functional Characteristics

| Aspect | Implementation |
|---|---|
| **Speed** | Pre-login reuse, judge/planning/thinking off by default, flash mode, capped timeouts/retries, `--budget` wall-clock cap, `--max-steps`, `--headless`. |
| **Resilience** | Version-tolerant imports, LLM param introspection (`inspect.signature`), graceful fallbacks, always-writes-report on budget expiry/interrupts. |
| **Determinism vs Adaptivity** | Deterministic path = gate of record; AI path = exploratory with guardrails (stop workarounds after repeated failure). |
| **Evidence** | Per-step screenshots (embedded or files), timeline, anomalies with evidence, collapsible tracebacks in HTML. |
| **Separation of Concerns** | Scenario/QA rules separate from steps; reporter shared; AI deps isolated (`requirements-agent.txt`) from the deterministic gate (`requirements.txt`). |
| **Extensibility** | Add new scenario `.md` files → both paths runnable; new providers via mapping; new renderers in `reporting/`. |

## 8. Directory Structure

```text
saucedemo-ai-qa-agent/
├── MODEL_ACCESS.md            # Model/provider config
├── requirements.txt           # Regression deps (pytest + playwright)
├── requirements-agent.txt     # AI deps (browser-use, rich)
├── pytest.ini
├── scenarios/                 # Single source of truth (*.md)
├── prompts/qa_system_prompt.md
├── config/load_model_access.py
├── agent/                     # AI path
│   ├── run_scenario.py
│   └── agent_setup.py
├── tools/                     # scenario -> test generator
│   └── generate_tests.py
├── tests/                     # Deterministic path
│   ├── conftest.py
│   ├── steps/step_helpers.py
│   └── generated/            # auto-generated tests + manifest.json
├── reporting/                 # Shared reporter
│   ├── model.py
│   ├── markdown.py
│   └── html.py
├── docs/                      # Architecture artifacts (this file + excalidraw)
├── .browser_profile/          # Persistent auth profile (git-ignored)
└── reports/                   # Outputs (git-ignored)
    ├── agent/<ts>/
    └── pytest/<ts>/
```

## 9. Excalidraw Mapping Guide

Use the companion `docs/aq-agent-architecture.excalidraw` with these layer mappings:

| Excalidraw Group/Frame | Components | Color Suggestion |
|---|---|---|
| **Input / Config** | CLI, `scenarios/*.md`, `MODEL_ACCESS.md`, `prompts/qa_system_prompt.md` | Blue (input/config) |
| **Orchestration** | `agent/run_scenario.py`, `agent/agent_setup.py`, `config/load_model_access.py` | Indigo (control/orchestration) |
| **AI Execution** | Browser Use Agent, LLM (Gemini/etc.), QA Rules application | Purple (reasoning/agent) |
| **Browser/Auth** | `BrowserSession`, `.browser_profile/`, Chromium | Cyan (browser/runtime) |
| **Target** | SauceDemo (www.saucedemo.com) | Orange (external system) |
| **Deterministic Path** | pytest, Playwright, `tools/generate_tests.py`, `tests/generated`, `tests/steps` | Green (deterministic/gate) |
| **Reporting (Shared)** | `reporting/*` (model/markdown/html) | Yellow (transformation/output) |
| **Artifacts** | `reports/agent/`, `reports/pytest/` | Gray (storage/artifacts) |
| **Flows** | Arrows: Scenario→Paths, AI→Browser→Target, Evidence→Reporter→Artifacts | Black/Gray (data/control) |

**Arrows to label clearly:** "loads scenario", "builds agent + session", "executes steps with observation", "captures per-step screenshots/evidence", "normalizes to RunReport", "renders MD+HTML", "generates pytest file (hash-gated)".

## 10. Quick Reference (Commands)

```powershell
# AI path (analyze + execute)
.\.venv\Scripts\python.exe agent\run_scenario.py --scenario scenarios\checkout_problem_user.md --headless --budget 600 --max-steps 20

# Generate deterministic tests (only when the scenario text changed)
.\.venv\Scripts\python.exe tools\generate_tests.py --all
.\.venv\Scripts\python.exe tools\generate_tests.py --all --check      # CI drift gate

# Deterministic gate (no model)
.\.venv\Scripts\python.exe -m pytest -v

# All three in one command
.\.venv\Scripts\python.exe agent\run_scenario.py --scenario scenarios\checkout_problem_user.md --generate-test --run-test
```

## 11. Design Notes

- **Single Source of Truth**: Both paths consume same scenario + QA rules → consistent expectations.
- **Gate of Record**: pytest suite is authoritative; AI path explains evidence when they differ.
- **Evidence-First**: Every step gets a screenshot; failure row highlighted; HTML is self-contained by default.
- **Speed by Design**: Reduced model calls, budget caps, headless, hash-gated test generation (no LLM call when nothing changed).
- **Model Flexibility**: Works with Gemini (native API) and any OpenAI-compatible endpoint; vision recommended for visual anomaly detection (e.g. identical product images on `problem_user`).
- **Safe Defaults**: Deterministic deps minimal; agent deps optional; runs always produce reports.

---
*Generated from codebase: `D:\Monika\OpenCode\Project\saucedemo-ai-qa-agent\`*