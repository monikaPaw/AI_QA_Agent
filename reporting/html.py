"""HTML report writer for BOTH paths (single source of report rendering).

Every scenario step is rendered as one row that shows, in the same place:
  step number + name -> PASS/FAIL badge -> observation/assertion -> screenshot

Screenshots are embedded as base64 data URIs by default so the report is a
single, self-contained file you can move or email (`embed=False` keeps them as
files next to the report instead).
"""
from __future__ import annotations

import base64
import mimetypes
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Environment

from .model import (
    FAILED,
    MIXED,
    NOT_RUN,
    PASSED,
    RECOVERY,
    SKIPPED,
    RunReport,
    TestResult,
    apply_recovery_status,
)

_ENV = Environment(autoescape=True)

_SOURCE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>{{ title }}</title>
<style>
  :root { --pass:#2e7d32; --fail:#c62828; --skip:#f9a825; --mixed:#ef6c00; --ink:#202124; --muted:#5f6368; --line:#e0e0e0; --bg:#f5f6f8; }
  * { box-sizing: border-box; }
  body { font-family:"Segoe UI",Roboto,Helvetica,Arial,sans-serif; margin:0; background:var(--bg); color:var(--ink); }
  .banner { padding:18px 28px; color:#fff; font-size:20px; font-weight:600; display:flex; justify-content:space-between; align-items:center; gap:16px; flex-wrap:wrap; }
  .banner.pass { background:var(--pass); } .banner.fail { background:var(--fail); }
  .banner .meta { font-size:13px; font-weight:400; opacity:.92; }
  .container { max-width:1100px; margin:24px auto; padding:0 16px 40px; }
  .card { background:#fff; border:1px solid var(--line); border-radius:8px; padding:18px 22px; margin-bottom:20px; box-shadow:0 1px 2px rgba(0,0,0,.06); }
  h1 { font-size:22px; margin:0 0 8px; } h2 { font-size:17px; margin:0 0 12px; border-bottom:1px solid #eee; padding-bottom:8px; }
  h3 { font-size:15px; margin:18px 0 8px; }
  p { color:var(--muted); margin:0 0 10px; }
  .meta-line { color:var(--muted); font-size:13px; line-height:1.7; }
  .kpis { display:flex; gap:10px; flex-wrap:wrap; margin-top:12px; }
  .kpi { border:1px solid var(--line); border-radius:6px; padding:6px 12px; font-size:13px; }
  .kpi strong { font-size:15px; }
  .pill { display:inline-block; padding:2px 10px; border-radius:999px; font-size:12px; font-weight:700; color:#fff; white-space:nowrap; }
  .pill.passed { background:var(--pass); } .pill.failed { background:var(--fail); }
  .pill.skipped { background:var(--skip); } .pill.mixed { background:var(--mixed); }
  .pill.not-run { background:#9aa0a6; }
  .pill.recovery { background:#8d6e63; }
  .test { border:1px solid var(--line); border-radius:8px; margin-bottom:18px; overflow:hidden; }
  .test > .head { display:flex; align-items:center; gap:12px; flex-wrap:wrap; padding:12px 16px; background:#fafafa; border-bottom:1px solid var(--line); }
  .test > .head .name { font-weight:600; font-size:15px; }
  .test > .head .dur, .test > .head .who { color:var(--muted); font-size:13px; }
  .test.failed { border-left:4px solid var(--fail); } .test.passed { border-left:4px solid var(--pass); }
  table { border-collapse:collapse; width:100%; font-size:14px; }
  th, td { border-bottom:1px solid var(--line); padding:10px 12px; text-align:left; vertical-align:top; }
  th { background:#fafafa; font-weight:600; font-size:13px; }
  td.num { width:34px; color:var(--muted); }
  td.step { width:230px; font-weight:600; }
  td.result { width:90px; }
  tr.failed { background:#fdf3f3; }
  tr.recovery { background:#faf7f5; }
  tr.recovery td { color:#6d4c41; }
  tr.not-run td { color:#9aa0a6; }
  .workaround { display:inline-block; margin-left:6px; padding:1px 8px; border-radius:999px; font-size:11px; font-weight:700; background:#eceff1; color:#37474f; border:1px solid #cfd8dc; }
  .detail { color:#3c4043; }
  .error { color:var(--fail); font-weight:600; margin-top:4px; }
  .url { color:var(--muted); font-size:12px; word-break:break-all; margin-top:4px; }
  img.thumb { display:block; margin-top:8px; max-width:320px; width:100%; border:1px solid #dadce0; border-radius:6px; cursor:zoom-in; }
  tr.failed img.thumb { max-width:560px; border-color:var(--fail); }
  details { padding:10px 16px 14px; }
  summary { cursor:pointer; color:var(--muted); font-size:13px; }
  pre { background:#1e1e1e; color:#d4d4d4; padding:14px; border-radius:6px; overflow-x:auto; font-size:12.5px; line-height:1.5; }
  code { background:#f1f3f4; padding:1px 5px; border-radius:4px; font-size:13px; }
  ul { padding-left:22px; color:#3c4043; } li { margin-bottom:6px; }
  .anomaly { border:1px solid var(--line); border-left:4px solid var(--mixed); border-radius:6px; padding:12px 16px; margin-bottom:10px; }
  footer { text-align:center; color:#9aa0a6; font-size:12px; padding:14px 0 0; }
  #lightbox { display:none; position:fixed; inset:0; background:rgba(0,0,0,.9); z-index:1000; cursor:zoom-out; }
  #lightbox img { max-width:94%; max-height:94%; margin:3% auto; display:block; border-radius:6px; }
</style>
</head>
<body>
<div class="banner {{ 'pass' if report.ok else 'fail' }}">
  <span>{{ "All tests passed" if report.ok else "Test run failed" }} &mdash; {{ title }}</span>
  <span class="meta">{{ counts }} &#183; {{ report.generated_at }}</span>
</div>

<div class="container">
  <section class="card">
    <h1>{{ title }}</h1>
    <div class="meta-line">
      Run ID: <strong>{{ report.run_id }}</strong><br/>
      Mode: <strong>{{ report.mode }}</strong>{% if report.framework %} &#183; Framework: <strong>{{ report.framework }}</strong>{% endif %}<br/>
      Target: <strong>{{ report.target }}</strong>
      {% if report.model %}<br/>Model: <strong>{{ report.model }}</strong>{% if report.provider %} ({{ report.provider }}){% endif %}{% endif %}
      {% if report.scenarios %}<br/>Scenarios: <strong>{{ report.scenarios|join(', ') }}</strong>{% endif %}
    </div>
    <div class="kpis">
      <span class="kpi">Verdict <strong>{{ report.verdict }}</strong></span>
      <span class="kpi">Passed <strong>{{ report.passed }}</strong></span>
      <span class="kpi">Failed <strong>{{ report.failed }}</strong></span>
      <span class="kpi">Skipped <strong>{{ report.skipped }}</strong></span>
      <span class="kpi">Duration <strong>{{ '%.2f'|format(report.duration) }}s</strong></span>
    </div>
  </section>

  {% if report.summary %}
  <section class="card"><h2>Summary</h2><p>{{ report.summary }}</p></section>
  {% endif %}

  {# what-it-did / automated-vs-human sections removed per request #}

  {% if report.anomalies %}
  <section class="card">
    <h2>Anomalies Found</h2>
    <p style="font-size:12px;color:var(--muted);margin:-4px 0 10px;">
      Reported by the AI agent in its own words; code only validates the structure and links the evidence.
    </p>
    {% for a in report.anomalies %}
    <div class="anomaly">
      <h3>{{ loop.index }}. {{ a.title }}</h3>
      <p><strong>Severity:</strong> {{ a.severity }}
      {% if a.scenario_step %}<br/><strong>Scenario step:</strong> {{ a.scenario_step }}{% endif %}
      {% if a.step %}<br/><strong>Agent step:</strong> {{ a.step }}{% endif %}
      {% if a.expected %}<br/><strong>Expected:</strong> {{ a.expected }}{% endif %}
      {% if a.observed %}<br/><strong>Observed:</strong> {{ a.observed }}{% endif %}
      {% if not a.expected and not a.observed %}<br/><strong>Description:</strong> {{ a.description }}{% endif %}
      {% if a.evidence %}<br/><strong>Evidence:</strong> {{ a.evidence }}{% endif %}
      {% if a.impact %}<br/><strong>Impact:</strong> {{ a.impact }}{% endif %}</p>
    </div>
    {% endfor %}
  </section>
  {% endif %}

  {% if report.integrity %}
  <section class="card">
    <h2>Run Integrity</h2>
    <p style="font-size:12px;color:var(--muted);margin:-4px 0 10px;">
      Objective checks on the run itself, not product findings.
    </p>
    {% for a in report.integrity %}
    <div class="anomaly" style="border-left-color:var(--fail);">
      <h3>{{ loop.index }}. {{ a.title }}</h3>
      <p><strong>Severity:</strong> {{ a.severity }}
      <br/><strong>Description:</strong> {{ a.description }}
      {% if a.evidence %}<br/><strong>Evidence:</strong> {{ a.evidence }}{% endif %}
      {% if a.impact %}<br/><strong>Impact:</strong> {{ a.impact }}{% endif %}</p>
    </div>
    {% endfor %}
  </section>
  {% endif %}

  <section class="card">
    <h2>Failures Found</h2>
    {% if failures_ctx %}
      {% for t in failures_ctx %}
      <h3>{{ loop.index }}. {{ t.name }}</h3>
      <p><strong>Severity:</strong> high<br/>
         <strong>Description:</strong> {{ t.assertion or 'Test failed' }}
         {% if t.step %}<br/><strong>Failed step:</strong> step {{ t.step.index }} &mdash; {{ t.step.name }}{% endif %}
         <br/><strong>Duration:</strong> {{ '%.2f'|format(t.duration) }}s
         <br/><strong>Impact:</strong> the checkout scenario cannot complete for this user account.</p>
      {% if t.img %}<img class="thumb" src="{{ t.img }}" alt="failure" loading="lazy" onclick="zoom(this.src)"/>{% endif %}
      {% endfor %}
    {% else %}
      <p>None detected.</p>
    {% endif %}
  </section>

  <section class="card">
    <h2>Step-by-Step Timeline &amp; Evidence</h2>
    <div class="note" style="font-size:12px;color:#5f6368;margin:-4px 0 10px;">
      <b>RECOVERY</b> = executed after the first failure, while the run was already failing - not a verified pass.
      <b>NON-USER WORKAROUND</b> = raw JavaScript used to force a value (a real user could not do this).
    </div>
    {% for t in tests_ctx %}
    <div class="test {{ t.outcome }}">
      <div class="head">
        <span class="name">{{ t.name }}</span>
        <span class="pill {{ t.outcome }}">{{ t.label }}</span>
        <span class="dur">{{ '%.2f'|format(t.duration) }}s</span>
        {% if t.user %}<span class="who">user: {{ t.user }}</span>{% endif %}
      </div>
      {% if t.scenario_steps %}
      <div style="font-size:12px;color:var(--muted);margin:0 0 8px 2px;">
        <strong>Scenario steps (agent's own assessment):</strong>
        <ul style="margin:4px 0 0 16px;padding:0;">{% for line in t.scenario_steps %}<li>{{ line }}</li>{% endfor %}</ul>
      </div>
      {% endif %}
      <table>
        <thead><tr><th>#</th><th>Step</th><th>Result</th><th>Evidence</th></tr></thead>
        <tbody>
        {% for s in t.steps %}
          <tr class="{{ s.row_class }}">
            <td class="num">{{ s.index }}</td>
            <td class="step">{{ s.name }}{% if s.workaround %}<span class="workaround">NON-USER WORKAROUND</span>{% endif %}</td>
            <td class="result"><span class="pill {{ s.status }}">{{ s.label }}</span></td>
            <td>
              {% if s.detail %}<div class="detail">{{ s.detail }}</div>{% endif %}
              {% if s.error %}<div class="error">{{ s.error }}</div>{% endif %}
              {% if s.url %}<div class="url">{{ s.url }}</div>{% endif %}
              {% if s.img %}<img class="thumb" src="{{ s.img }}" alt="step {{ s.index }}" loading="lazy" onclick="zoom(this.src)"/>{% endif %}
            </td>
          </tr>
        {% endfor %}
        </tbody>
      </table>
      {% if t.traceback %}<details><summary>Traceback</summary><pre>{{ t.traceback }}</pre></details>{% endif %}
    </div>
    {% endfor %}
  </section>

  <footer>Generated by saucedemo-ai-qa-agent &#183; {{ report.generated_at }}</footer>
</div>

<div id="lightbox" onclick="this.style.display='none'"><img id="lightbox-img" alt="screenshot"/></div>
<script>
  function zoom(src){const b=document.getElementById('lightbox');document.getElementById('lightbox-img').src=src;b.style.display='block';}
</script>
</body>
</html>"""

_LABELS = {
    PASSED: "PASSED",
    FAILED: "FAILED",
    SKIPPED: "SKIPPED",
    MIXED: "MIXED",
    RECOVERY: "RECOVERY",
    NOT_RUN: "NOT RUN",
}


def _img_src(out_dir: Path, rel_path: Optional[str], embed: bool) -> Optional[str]:
    if not rel_path:
        return None
    path = Path(rel_path)
    if not path.is_absolute():
        path = Path(out_dir) / rel_path
    if not path.exists():
        return None
    if embed:
        mime = mimetypes.guess_type(str(path))[0] or "image/png"
        return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")
    return rel_path.replace("\\", "/")


def _step_ctx(step, out_dir: Path, embed: bool) -> Dict[str, Any]:
    # `step` is the dict produced by apply_recovery_status (already re-labelled).
    return {
        "index": step["index"],
        "name": step["name"],
        "status": step["status"],
        "label": _LABELS.get(step["status"], step["status"].upper()),
        "detail": step["detail"],
        "error": step["error"],
        "url": step["url"],
        "workaround": bool(step["workaround"]),
        "row_class": step["status"] if step["status"] in (FAILED, NOT_RUN, RECOVERY) else "",
        "img": _img_src(out_dir, step["screenshot"], embed),
    }


def render_html(report: RunReport, out_dir: Path, embed: bool = True, title: str = "SauceDemo QA Test Report") -> str:
    out_dir = Path(out_dir)
    counts = " &#183; ".join(
        part
        for part in (
            f"{report.passed} passed" if report.passed else "",
            f"{report.failed} failed" if report.failed else "",
            f"{report.skipped} skipped" if report.skipped else "",
        )
        if part
    ) or "no results"

    tests_ctx: List[Dict[str, Any]] = []
    for test in report.tests:
        tests_ctx.append(
            {
                "name": test.name,
                "outcome": test.outcome,
                "label": _LABELS.get(test.outcome, test.outcome.upper()),
                "duration": test.duration,
                "user": test.user,
                "traceback": test.traceback or test.assertion,
                "scenario_steps": test.scenario_steps,
                "steps": [_step_ctx(s, out_dir, embed) for s in apply_recovery_status(test)],
            }
        )

    failures = [t for t in report.tests if t.outcome in (FAILED, MIXED)]
    failures_ctx = [
        {
            "name": test.name,
            "assertion": test.assertion,
            "step": test.failed_step,
            "duration": test.duration,
            "img": _img_src(out_dir, test.failure_screenshot, embed),
        }
        for test in failures
    ]
    template = _ENV.from_string(_SOURCE)
    return template.render(
        title=title,
        report=report,
        counts=counts,
        tests_ctx=tests_ctx,
        failures_ctx=failures_ctx,
    )


def write_html(
    report: RunReport,
    out_dir: Path,
    embed: bool = True,
    title: str = "SauceDemo QA Test Report",
    filename: str = "index.html",
) -> Path:
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / filename
    path.write_text(render_html(report, out_dir, embed=embed, title=title), encoding="utf-8")
    return path
