import os
import re
from pathlib import Path
from typing import Dict, Any, Optional

# Load the project-local .env (if present) so API keys can live outside
# MODEL_ACCESS.md. Real environment variables always win (override=False).
# python-dotenv ships with requirements-agent.txt; the deterministic pytest
# gate does not need it, so a missing package is not fatal.
try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env", override=False)
except ImportError:  # pragma: no cover - gate-only installs
    pass


class ModelAccessConfig:
    """Reads MODEL_ACCESS.md and turns the 'Active Configuration' section into settings."""

    # default values
    DEFAULTS: Dict[str, Any] = {
        "provider": "openrouter",
        "model": "qwen/qwen2.5-vl-72b-instruct",
        "base_url": "https://openrouter.ai/api/v1",
        "api_key": "",
        "vision": True,
        "temperature": 0.1,
        "max_tokens": 4096,
        "max_steps": 40,
        "llm_timeout": 180,
        "add_schema_to_system_prompt": True,
        "headless": False,
        "browser": "chromium",
        "save_screenshots": True,
        "save_agent_history": True,
        # --- speed / reliability profile -----------------------------------
        # These only apply to the AI agent path. Defaults are tuned so a run
        # finishes in a few minutes instead of "hanging" on a slow free model.
        "use_judge": False,          # judge = an EXTRA LLM call after every step
        "enable_planning": False,    # planning adds more LLM calls per run
        "flash_mode": True,          # action-only output: far fewer tokens
        "use_thinking": False,       # no long reasoning blocks
        "max_actions_per_step": 5,   # allow batching several actions in one call
        "max_failures": 2,           # stop early instead of retrying 5x
        "step_timeout": 90,          # fail a wedged step fast
        "loop_detection_enabled": True,
        "loop_detection_window": 8,  # bail out of a loop quickly
        "wait_between_actions": 0.0,
        "min_page_load_wait": 0.1,
        "network_idle_wait": 0.25,
        "demo_budget": 420,          # hard wall-clock cap in seconds (0 = off)
        "llm_max_retries": 2,
    }

    def __init__(self, config_path: Optional[Path] = None):
        self.project_root = Path(__file__).parent.parent
        self.config_path = config_path or self.project_root / "MODEL_ACCESS.md"
        self.config = dict(self.DEFAULTS)
        self.config.update(self._parse_md())

    # -- parsing helpers -----------------------------------------------------
    @staticmethod
    def _clean(value: str) -> str:
        """Remove inline markdown comments and surrounding whitespace/quotes."""
        # strip markdown comment syntax <!-- ... -->
        value = re.sub(r"<!--.*?-->", "", value)
        # strip everything after a '#' comment (leading, or preceded by space)
        value = re.split(r"(?:^|\s+)#", value, maxsplit=1)[0]
        return value.strip().strip('"').strip("'").strip()

    def _section(self, text: str, name: str) -> str:
        """Return the body of a '## name' section up to the next '## ' heading."""
        pattern = re.compile(rf"^##\s+{re.escape(name)}\s*$(.*?)(?=^##\s+|\Z)",
                             re.MULTILINE | re.DOTALL | re.IGNORECASE)
        m = pattern.search(text)
        return m.group(1) if m else ""

    def _field(self, body: str, key: str) -> Optional[str]:
        m = re.search(rf"^\s*-?\s*{re.escape(key)}\s*:\s*(.*)$", body, re.MULTILINE | re.IGNORECASE)
        if not m:
            return None
        return self._clean(m.group(1))

    def _parse_md(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            return {}
        cfg: Dict[str, Any] = {}
        try:
            text = self.config_path.read_text(encoding="utf-8")
            # Prefer the Active Configuration section; fall back to whole doc.
            body = self._section(text, "Active Configuration") or text

            provider = self._field(body, "Provider")
            if provider:
                cfg["provider"] = provider.split()[0].lower() if " " not in provider else provider.lower()

            model = self._field(body, "Model")
            if model:
                cfg["model"] = model

            base_url = self._field(body, "Base URL")
            if base_url:
                cfg["base_url"] = base_url

            api_key = self._field(body, "API Key")
            if api_key is not None:
                cfg["api_key"] = api_key

            vision = self._field(body, "Vision")
            if vision:
                cfg["vision"] = vision.lower() == "true"

            temp = self._field(body, "Temperature")
            if temp:
                try:
                    cfg["temperature"] = float(temp)
                except ValueError:
                    pass

            steps = self._field(body, "Max Steps")
            if steps and steps.isdigit():
                cfg["max_steps"] = int(steps)

            max_tokens = self._field(body, "Max Tokens")
            if max_tokens and max_tokens.isdigit():
                cfg["max_tokens"] = int(max_tokens)

            llm_timeout = self._field(body, "LLM Timeout")
            if llm_timeout and llm_timeout.isdigit():
                cfg["llm_timeout"] = int(llm_timeout)

            schema_prompt = self._field(body, "Add Schema To Prompt")
            if schema_prompt:
                cfg["add_schema_to_system_prompt"] = schema_prompt.lower() == "true"

            headless = self._field(body, "Headless")
            if headless:
                cfg["headless"] = headless.lower() == "true"

            browser = self._field(body, "Browser")
            if browser:
                b = browser.split()[0].lower()
                if b in ("chromium", "firefox", "webkit"):
                    cfg["browser"] = b

            ss = self._field(body, "Save Screenshots")
            if ss:
                cfg["save_screenshots"] = ss.lower() == "true"

            ah = self._field(body, "Save Agent History")
            if ah:
                cfg["save_agent_history"] = ah.lower() == "true"

            def _bool(key):
                v = self._field(body, key)
                return None if v is None else v.lower() == "true"

            def _int(key):
                v = self._field(body, key)
                if v is None:
                    return None
                try:
                    return int(float(v))
                except ValueError:
                    return None

            def _float(key):
                v = self._field(body, key)
                if v is None:
                    return None
                try:
                    return float(v)
                except ValueError:
                    return None

            bool_fields = {
                "Use Judge": "use_judge",
                "Enable Planning": "enable_planning",
                "Flash Mode": "flash_mode",
                "Use Thinking": "use_thinking",
                "Loop Detection": "loop_detection_enabled",
            }
            for md_key, cfg_key in bool_fields.items():
                value = _bool(md_key)
                if value is not None:
                    cfg[cfg_key] = value

            int_fields = {
                "Max Actions Per Step": "max_actions_per_step",
                "Max Failures": "max_failures",
                "Step Timeout": "step_timeout",
                "Loop Detection Window": "loop_detection_window",
                "Demo Budget Seconds": "demo_budget",
                "LLM Max Retries": "llm_max_retries",
            }
            for md_key, cfg_key in int_fields.items():
                value = _int(md_key)
                if value is not None:
                    cfg[cfg_key] = value

            float_fields = {
                "Wait Between Actions": "wait_between_actions",
                "Min Page Load Wait": "min_page_load_wait",
                "Network Idle Wait": "network_idle_wait",
            }
            for md_key, cfg_key in float_fields.items():
                value = _float(md_key)
                if value is not None:
                    cfg[cfg_key] = value
        except Exception as e:  # pragma: no cover
            print(f"Warning: failed parsing MODEL_ACCESS.md: {e}")
        return cfg

    # -- environment ---------------------------------------------------------
    def resolve_api_key(self) -> str:
        key = self.config.get("api_key", "")
        if key:
            return key
        provider = self.config.get("provider", "")
        env_map = {
            "openrouter": ["OPENROUTER_API_KEY", "OPENAI_API_KEY"],
            "openai": ["OPENAI_API_KEY"],
            "anthropic": ["ANTHROPIC_API_KEY"],
            "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
            "groq": ["GROQ_API_KEY"],
            "cerebras": ["CEREBRAS_API_KEY"],
            "custom": ["OPENAI_API_KEY", "API_KEY"],
            "litellm": ["OPENAI_API_KEY", "API_KEY"],
        }
        for name in env_map.get(provider, ["OPENAI_API_KEY"]):
            if os.getenv(name):
                return os.getenv(name)
        return ""

    def apply_to_env(self):
        """Expose configuration through environment variables for Browser Use.

        Note: we never write a placeholder API key here. The API key is passed
        directly to the LLM object; a fake value in the environment would hide a
        genuinely missing key.
        """
        api_key = self.resolve_api_key()
        base_url = self.config.get("base_url", "")
        provider = self.config.get("provider", "")

        if api_key and not os.environ.get("OPENAI_API_KEY"):
            os.environ["OPENAI_API_KEY"] = api_key
        if base_url:
            # Useful for OpenAI-compatible providers (OpenRouter, Groq, Cerebras, custom).
            os.environ.setdefault("OPENAI_BASE_URL", base_url)
        os.environ.setdefault("OPENROUTER_API_KEY", api_key) if (api_key and provider == "openrouter") else None

        os.environ["BROWSER_USE_HEADLESS"] = "1" if self.config.get("headless") else "0"
        os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")

    def to_dict(self) -> Dict[str, Any]:
        return dict(self.config)
