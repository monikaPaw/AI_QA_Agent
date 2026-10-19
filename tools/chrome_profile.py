"""Seed Chrome profile preferences that disable password-manager popups.

Headed automation on SauceDemo triggers two browser-UI popups that cover the
page and swallow clicks:

1. "Change your password" - Chrome's password-leak warning; the demo password
   ``secret_sauce`` is in public breach corpora, so Chrome raises it on login.
2. "Save username and password?" - the save-password bubble.

Both are driven by profile preferences, so they are switched off by writing
``<user_data_dir>/Default/Preferences`` *before* Chromium starts. Chromium
reads and keeps these prefs (issue 42323769 confirms all three are needed:
``credentials_enable_service`` and ``profile.password_manager_enabled`` alone
no longer suppress the breach warning).

Used by the agent path (agent/agent_setup.py, which gives browser-use a fresh
seeded temp profile before it launches) and the deterministic gate
(tests/conftest.py, seeded into a fresh Playwright persistent profile per
test).
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Dict

# Prefs that turn the whole password-manager stack off. The leak-detection
# pref is written in both spellings seen in the wild (top-level and profile.*)
# so it takes regardless of how the Chromium version registers it.
PASSWORD_MANAGER_OFF: Dict[str, Any] = {
    "credentials_enable_service": False,
    "password_manager_leak_detection": False,
    "profile": {
        "password_manager_enabled": False,
        "password_manager_leak_detection": False,
    },
}


def seed_password_prefs(profile_dir: Path) -> Path:
    """Write Default/Preferences into profile_dir so Chromium shows no password popups.

    Merges into an existing Preferences file (keeps unrelated keys) and is a
    no-op failure-wise if the directory cannot be written - callers pass a
    profile dir Chromium has not started with yet, so this is always a fresh
    ``Default`` folder in practice.
    """
    profile_dir = Path(profile_dir)
    default_dir = profile_dir / "Default"
    default_dir.mkdir(parents=True, exist_ok=True)
    prefs_path = default_dir / "Preferences"

    prefs: Dict[str, Any] = {}
    if prefs_path.exists():
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            prefs = {}

    for key, value in PASSWORD_MANAGER_OFF.items():
        if isinstance(value, dict) and isinstance(prefs.get(key), dict):
            prefs[key] = {**prefs[key], **value}  # keep unrelated profile keys
        else:
            prefs[key] = value

    prefs_path.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    return profile_dir


# browser-use names its own temp profile dirs with this marker and its
# _copy_profile() uses such directories as-is (never clones them), so our
# seeded dir carries it too - see browser_use/browser/profile.py.
BROWSER_USE_TEMP_PREFIX = "browser-use-user-data-dir-"


def make_seeded_profile_dir() -> Path:
    """Create a fresh temp profile dir seeded with PASSWORD_MANAGER_OFF.

    One new directory per run/test preserves the isolation semantics of the
    old flows (browser-use's validator-created dir, Playwright's
    new_context()), while the seeded prefs keep the popups away. The name
    carries browser-use's temp-dir marker so browser-use launches straight
    from it instead of cloning the profile elsewhere.
    """
    profile_dir = Path(tempfile.mkdtemp(prefix=BROWSER_USE_TEMP_PREFIX))
    return seed_password_prefs(profile_dir)
