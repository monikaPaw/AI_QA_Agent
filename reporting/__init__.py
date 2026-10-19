"""One reporting package used by BOTH execution paths.

- `model`    : normalised run/step/test result objects
- `markdown` : report.md writer
- `html`     : index.html writer (per-step result + screenshot + evidence, embedded)
"""
from .model import (  # noqa: F401
    FAILED,
    MIXED,
    NOT_RUN,
    PASSED,
    RECOVERY,
    SKIPPED,
    RunReport,
    StepResult,
    TestResult,
    apply_recovery_status,
)
from .markdown import render_markdown, write_markdown  # noqa: F401
from .html import render_html, write_html  # noqa: F401

__all__ = [
    "RunReport",
    "StepResult",
    "TestResult",
    "PASSED",
    "FAILED",
    "SKIPPED",
    "MIXED",
    "NOT_RUN",
    "RECOVERY",
    "apply_recovery_status",
    "render_markdown",
    "write_markdown",
    "render_html",
    "write_html",
]
