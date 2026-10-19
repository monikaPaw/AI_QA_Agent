"""Generate a deterministic pytest file from a scenario Markdown file.

The LLM is used ONLY here, at authoring time. Normal test runs execute the
generated pytest file with no model involved, so runs stay deterministic.

Generation is keyed by a SHA-256 hash of the scenario file: it happens once and
is re-run only when the scenario's requirements change (or with --force).

Usage:
    python tools/generate_tests.py --scenario scenarios/abc.md
    python tools/generate_tests.py --scenario scenarios/abc.md --force
    python tools/generate_tests.py --scenario scenarios/abc.md --dry-run
    python tools/generate_tests.py --scenario scenarios/abc.md --check
    python tools/generate_tests.py --all
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCENARIOS_DIR = PROJECT_ROOT / "scenarios"
OUT_DIR = PROJECT_ROOT / "tests" / "generated"
MANIFEST = OUT_DIR / "manifest.json"
HELPERS = PROJECT_ROOT / "tests" / "steps" / "step_helpers.py"
EXAMPLE = PROJECT_ROOT / "tests" / "generated" / "test_checkout_standard_user.py"


# --------------------------------------------------------------------- helpers
def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def scenario_hash(text: str) -> str:
    """Hash the scenario's *meaning*: ignore trailing whitespace and line endings."""
    normalised = "\n".join(line.rstrip() for line in text.replace("\r\n", "\n").split("\n")).strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


def load_manifest() -> dict:
    if MANIFEST.exists():
        try:
            return json.loads(_read(MANIFEST))
        except Exception:  # noqa: BLE001
            return {}
    return {}


def save_manifest(data: dict) -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    MANIFEST.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def rel(path: Path) -> str:
    return path.relative_to(PROJECT_ROOT).as_posix()


def test_path_for(scenario_path: Path) -> Path:
    """Where the generated pytest file for a scenario lives."""
    return OUT_DIR / f"test_{scenario_path.stem}.py"


# ----------------------------------------------------------------------- model
def _llm():
    from config.load_model_access import ModelAccessConfig
    from agent.agent_setup import AgentBuilder

    cfg = ModelAccessConfig().config
    builder = AgentBuilder(cfg, PROJECT_ROOT)
    llm = builder._build_llm(builder._imports()["llms"])
    return llm, cfg


def _call_llm(llm, prompt: str) -> str:
    from browser_use.llm.messages import UserMessage

    async def run():
        result = await llm.ainvoke([UserMessage(content=prompt)])
        return result.completion

    out = asyncio.run(run())
    if not isinstance(out, str):
        out = str(out)
    return out


# ---------------------------------------------------------------------- prompt
def build_prompt(scenario_text: str, test_name: str) -> str:
    return (
        "You are a senior test engineer. Convert the scenario below into ONE\n"
        "deterministic Python file for the pytest + Playwright stack.\n\n"
        "HARD RULES\n"
        "- A `page` fixture is already available; do not create browsers yourself.\n"
        "- Reuse the shared helpers in tests/steps/step_helpers.py wherever they fit; import only what you use.\n"
        f"- Define exactly ONE test function named `{test_name}(page)`.\n"
        "- Assertions MUST come from the scenario's 'Expected results' section; do not invent behaviour.\n"
        "- Deterministic: no randomness, no sleeps, no network calls other than the target site.\n"
        "- Output ONLY the Python file contents. No prose, no markdown code fences.\n\n"
        "=== SCENARIO FILE ===\n"
        f"{scenario_text}\n\n"
        "=== AVAILABLE HELPERS (tests/steps/step_helpers.py) ===\n"
        f"{_read(HELPERS)}\n\n"
        "=== STYLE EXAMPLE (tests/generated/test_checkout_standard_user.py) ===\n"
        f"{_read(EXAMPLE)}\n\n"
        f"Now write {test_name}(page) in exactly that style.\n"
    )


# ---------------------------------------------------------------------- output
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9]*\s*$")


def strip_fences(text: str) -> str:
    lines = text.replace("\r\n", "\n").split("\n")
    if lines and _FENCE.match(lines[0]):
        lines = lines[1:]
    if lines and _FENCE.match(lines[-1]):
        lines = lines[:-1]
    return "\n".join(lines).strip("\n") + "\n"


def validate(code: str, test_name: str) -> None:
    tree = ast.parse(code)  # raises SyntaxError if invalid
    funcs = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    if test_name not in funcs:
        raise ValueError(f"generated code does not define {test_name}(); found {funcs}")


# ------------------------------------------------------------------------ flow
def generate(scenario_path: Path, force: bool, dry_run: bool) -> int:
    scenario_text = _read(scenario_path)
    digest = scenario_hash(scenario_text)
    stem = scenario_path.stem
    test_name = f"test_{stem}"
    out_path = test_path_for(scenario_path)
    key = rel(scenario_path)

    manifest = load_manifest()
    entry = manifest.get(key, {})
    if not force and entry.get("sha256") == digest and out_path.exists():
        print(f"up to date: {key} (hash unchanged) -> {rel(out_path)}")
        return 0

    print(f"generating {rel(out_path)} from {key} ...")
    llm, cfg = _llm()
    raw = _call_llm(llm, build_prompt(scenario_text, test_name))
    code = strip_fences(raw)
    try:
        validate(code, test_name)
    except (SyntaxError, ValueError) as exc:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / ".last_rejected.txt").write_text(raw, encoding="utf-8")
        print(f"ERROR: generated code failed validation: {exc}")
        print(f"raw output saved to {rel(OUT_DIR / '.last_rejected.txt')}")
        return 2

    header = (
        f"# AUTO-GENERATED from {key} - do not edit by hand.\n"
        f"# Regenerate: python tools/generate_tests.py --scenario {key} --force\n"
    )
    content = header + code

    if dry_run:
        print("--- dry run: generated file follows ---")
        print(content)
        return 0

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    existing = _read(out_path) if out_path.exists() else None
    if existing == content:
        print(f"unchanged: {rel(out_path)} (LLM produced identical output)")
    else:
        out_path.write_text(content, encoding="utf-8")
        print(f"wrote: {rel(out_path)}")

    manifest[key] = {
        "sha256": digest,
        "test_file": rel(out_path),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "provider": cfg.get("provider", ""),
        "model": cfg.get("model", ""),
    }
    save_manifest(manifest)
    print(f"manifest updated: {rel(MANIFEST)}")
    return 0


def check(scenario_path: Path) -> int:
    scenario_text = _read(scenario_path)
    digest = scenario_hash(scenario_text)
    key = rel(scenario_path)
    entry = load_manifest().get(key, {})
    test_file = PROJECT_ROOT / entry.get("test_file", OUT_DIR / f"test_{scenario_path.stem}.py")
    if entry.get("sha256") == digest and test_file.exists():
        print(f"in sync: {key}")
        return 0
    if not entry:
        reason = "no generated test yet"
    elif entry.get("sha256") != digest:
        reason = "scenario changed"
    else:
        reason = "test file missing"
    print(f"OUT OF DATE: {key} ({reason}); run tools/generate_tests.py --scenario {key}")
    return 1


def _scenarios(args) -> list:
    if args.all:
        return sorted(SCENARIOS_DIR.glob("*.md"))
    return [Path(args.scenario).resolve()]


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate deterministic pytest files from scenario Markdown.")
    parser.add_argument("--scenario", help="path to a scenario file, e.g. scenarios/abc.md")
    parser.add_argument("--all", action="store_true", help="every scenario in scenarios/")
    parser.add_argument("--force", action="store_true", help="regenerate even if the hash is unchanged")
    parser.add_argument("--dry-run", action="store_true", help="print the generated file, write nothing")
    parser.add_argument("--check", action="store_true", help="exit 1 if any scenario is out of date (for CI)")
    args = parser.parse_args()

    if not args.all and not args.scenario:
        parser.error("pass --scenario PATH or --all")

    rc = 0
    for path in _scenarios(args):
        if not path.exists():
            print(f"ERROR: scenario not found: {path}")
            rc = 2
            continue
        if args.check:
            rc = max(rc, check(path))
        else:
            rc = max(rc, generate(path, args.force, args.dry_run))
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
