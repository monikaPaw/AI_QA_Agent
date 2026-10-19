"""Builds a Browser Use agent and runs it, tolerating API differences between versions.

Primary target: browser-use 0.13.x
  - Agent          from browser_use.agent.service
  - BrowserSession from browser_use.browser
  - Chat* LLMs     from browser_use.llm
  - Agent.run(max_steps=...) is async

The scenario is supplied by the caller (see `agent/run_scenario.py`); this module
only knows how to build an agent for a given task + system prompt.
"""
import asyncio
import inspect
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Tuple


class AgentBuilder:
    def __init__(self, config: Dict[str, Any], project_root: Path = None):
        self.config = config
        self.project_root = project_root or Path(__file__).resolve().parent.parent
        self.prompt_path = self.project_root / "prompts" / "qa_system_prompt.md"

    # ------------------------------------------------------------------ imports
    def _imports(self):
        try:
            import browser_use
        except Exception as e:  # pragma: no cover
            raise ImportError(
                "browser-use is not installed.\n"
                "  pip install -r requirements-agent.txt"
            ) from e

        mods: Dict[str, Any] = {}

        try:
            from browser_use.agent.service import Agent
        except Exception:
            try:
                from browser_use import Agent  # older versions
            except Exception as e:
                raise ImportError(f"Could not import Agent from browser-use: {e}") from e
        mods["Agent"] = Agent

        try:
            from browser_use.browser import BrowserSession, BrowserProfile
        except Exception:
            try:
                from browser_use import Browser
                BrowserSession, BrowserProfile = Browser, None
            except Exception:
                BrowserSession, BrowserProfile = None, None
        mods["BrowserSession"] = BrowserSession
        mods["BrowserProfile"] = BrowserProfile

        llms: Dict[str, Any] = {}
        try:
            import browser_use.llm as llm_mod
            for name in ("ChatOpenAI", "ChatOpenRouter", "ChatGroq", "ChatCerebras",
                         "ChatAnthropic", "ChatGoogle", "ChatOllama"):
                if hasattr(llm_mod, name):
                    llms[name] = getattr(llm_mod, name)
        except Exception:
            pass
        if not llms:
            try:
                from browser_use import ChatOpenAI  # very old layout
                llms["ChatOpenAI"] = ChatOpenAI
            except Exception:
                pass
        mods["llms"] = llms

        try:
            version = __import__("importlib.metadata", fromlist=["version"]).version("browser-use")
        except Exception:
            version = getattr(browser_use, "__version__", "unknown")
        mods["version"] = version
        return mods

    # --------------------------------------------------------------------- llm
    def _build_llm(self, llms: Dict[str, Any]):
        provider = (self.config.get("provider") or "openrouter").lower()
        model = self.config["model"]
        api_key = self.config.get("api_key") or self.config.get("_resolved_key") or "dummy"
        base_url = self.config.get("base_url") or None
        temperature = self.config.get("temperature", 0.1)

        provider_class = {
            "openrouter": "ChatOpenRouter",
            "openai": "ChatOpenAI",
            "groq": "ChatGroq",
            "cerebras": "ChatCerebras",
            "anthropic": "ChatAnthropic",
            "gemini": "ChatGoogle",
            "google": "ChatGoogle",
            "custom": "ChatOpenAI",
            "litellm": "ChatOpenAI",
            "ollama": "ChatOllama",
        }.get(provider, "ChatOpenAI")

        cls = llms.get(provider_class) or llms.get("ChatOpenAI")
        if cls is None:
            return model  # let Browser Use resolve by name

        # Many free / weaker models do not support response_format (JSON schema).
        # Browser Use can instead embed the schema in the system prompt and parse
        # the JSON from plain text - but only the OpenAI-compatible class exposes
        # that option, so route through it when requested.
        want_schema_prompt = bool(self.config.get("add_schema_to_system_prompt"))
        if want_schema_prompt and "add_schema_to_system_prompt" not in self._params(cls):
            if llms.get("ChatOpenAI") is not None:
                cls = llms["ChatOpenAI"]

        kwargs: Dict[str, Any] = {"model": model}
        params = self._params(cls)

        if "api_key" in params:
            kwargs["api_key"] = api_key
        if base_url and "base_url" in params:
            if provider in ("custom", "litellm") or "openrouter.ai" in base_url or "openai.com" not in base_url:
                kwargs["base_url"] = base_url
        if "temperature" in params:
            kwargs["temperature"] = temperature

        # Cap response size so requests fit limited OpenRouter balances.
        max_tokens = self.config.get("max_tokens")
        if max_tokens:
            if "max_completion_tokens" in params:
                kwargs["max_completion_tokens"] = int(max_tokens)
            elif "max_tokens" in params:
                kwargs["max_tokens"] = int(max_tokens)
            elif "extra_body" in params:
                kwargs["extra_body"] = {"max_tokens": int(max_tokens)}

        if "add_schema_to_system_prompt" in params:
            kwargs["add_schema_to_system_prompt"] = bool(self.config.get("add_schema_to_system_prompt"))

        if "max_retries" in params and self.config.get("llm_max_retries") is not None:
            try:
                kwargs["max_retries"] = int(self.config["llm_max_retries"])
            except (TypeError, ValueError):
                pass

        try:
            return cls(**kwargs)
        except TypeError:
            kwargs.pop("temperature", None)
            kwargs.pop("base_url", None)
            try:
                return cls(**kwargs)
            except Exception:
                return cls(model=model, api_key=api_key)

    @staticmethod
    def _params(cls) -> set:
        try:
            return set(inspect.signature(cls.__init__).parameters)
        except (TypeError, ValueError):
            return set()

    # ----------------------------------------------------------------- browser
    def _build_browser_session(self, BrowserSession, BrowserProfile):
        headless = bool(self.config.get("headless", False))
        viewport = {"width": 1280, "height": 900}
        if BrowserSession is None:
            return None
        profile = None
        if BrowserProfile is not None:
            # highlight_elements draws an overlay before screenshots, which is slow on
            # some machines; disabling it avoids screenshot-watchdog timeouts.
            fields = getattr(BrowserProfile, "model_fields", None)
            pf = set(fields.keys()) if fields else set()
            profile_kwargs: Dict[str, Any] = {
                "headless": headless,
                "viewport": viewport,
                "highlight_elements": False,
            }
            for field, cfg_key in (
                ("wait_between_actions", "wait_between_actions"),
                ("minimum_wait_page_load_time", "min_page_load_wait"),
                ("wait_for_network_idle_page_load_time", "network_idle_wait"),
            ):
                value = self.config.get(cfg_key)
                if value is not None and (not pf or field in pf):
                    try:
                        profile_kwargs[field] = float(value)
                    except (TypeError, ValueError):
                        pass

            try:
                profile = BrowserProfile(**profile_kwargs)
            except Exception:
                try:
                    profile = BrowserProfile(headless=headless, viewport=viewport, highlight_elements=False)
                except Exception:
                    try:
                        profile = BrowserProfile(headless=headless, viewport=viewport)
                    except Exception:
                        profile = None
        if profile is not None and hasattr(profile, "user_data_dir"):
            # Chrome's password-manager popups (the "Change your password"
            # breach warning + the save-password bubble) cover the page and
            # swallow clicks, so Chromium must start from a pre-seeded profile.
            # This has to happen BEFORE BrowserSession(...) below: it copies the
            # profile, and revalidation would otherwise turn user_data_dir=None
            # into a fresh, unseeded temp dir.
            try:
                from tools.chrome_profile import make_seeded_profile_dir, seed_password_prefs
            except ImportError:
                sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
                from tools.chrome_profile import make_seeded_profile_dir, seed_password_prefs
            try:
                profile.user_data_dir = (
                    seed_password_prefs(Path(profile.user_data_dir))
                    if profile.user_data_dir
                    else make_seeded_profile_dir()
                )
            except Exception:
                pass  # popups are annoying but seeding must never break a run
        if profile is not None:
            try:
                return BrowserSession(browser_profile=profile)
            except TypeError:
                pass
        try:
            return BrowserSession(headless=headless, viewport=viewport)
        except TypeError:
            try:
                return BrowserSession(headless=headless)
            except Exception:
                return BrowserSession()

    # ------------------------------------------------------------------- agent
    def build_agent(
        self,
        task: str,
        system_prompt: Optional[str] = None,
        initial_actions: Optional[list] = None,
        step_callback=None,
        should_stop=None,
    ) -> Tuple[Any, Dict[str, Any]]:
        mods = self._imports()
        Agent = mods["Agent"]

        if system_prompt is None:
            system_prompt = (
                "You are an AI-driven QA agent for SauceDemo. Be thorough, skeptical and evidence-based."
            )
            if self.prompt_path.exists():
                system_prompt = self.prompt_path.read_text(encoding="utf-8")

        llm = self._build_llm(mods["llms"])
        browser_session = self._build_browser_session(mods["BrowserSession"], mods["BrowserProfile"])
        max_steps = int(self.config.get("max_steps", 40))

        agent_kwargs: Dict[str, Any] = {"task": task, "llm": llm}
        params = self._params(Agent)

        if browser_session is not None:
            if "browser_session" in params:
                agent_kwargs["browser_session"] = browser_session
            elif "browser" in params:
                agent_kwargs["browser"] = browser_session

        if "extend_system_message" in params:
            agent_kwargs["extend_system_message"] = system_prompt
        elif "override_system_message" in params:
            agent_kwargs["override_system_message"] = system_prompt
        elif "system_prompt" in params:
            agent_kwargs["system_prompt"] = system_prompt
        else:
            agent_kwargs["task"] = system_prompt + "\n\n---\n\nTASK:\n" + task

        if "use_vision" in params:
            agent_kwargs["use_vision"] = bool(self.config.get("vision", True))
        if "max_steps" in params:  # older versions took max_steps in the constructor
            agent_kwargs["max_steps"] = max_steps
        if "llm_timeout" in params and self.config.get("llm_timeout"):
            agent_kwargs["llm_timeout"] = int(self.config["llm_timeout"])

        # Speed / reliability knobs. Each is applied only when the installed
        # browser-use version actually exposes it, so this stays version tolerant.
        knobs = {
            "flash_mode": self.config.get("flash_mode"),
            "use_thinking": self.config.get("use_thinking"),
            "use_judge": self.config.get("use_judge"),
            "enable_planning": self.config.get("enable_planning"),
            "max_actions_per_step": self.config.get("max_actions_per_step"),
            "max_failures": self.config.get("max_failures"),
            "step_timeout": self.config.get("step_timeout"),
            "loop_detection_enabled": self.config.get("loop_detection_enabled"),
            "loop_detection_window": self.config.get("loop_detection_window"),
        }
        for key, value in knobs.items():
            if key in params and value is not None:
                agent_kwargs[key] = value

        if initial_actions and "initial_actions" in params:
            agent_kwargs["initial_actions"] = initial_actions
        if step_callback is not None and "register_new_step_callback" in params:
            agent_kwargs["register_new_step_callback"] = step_callback
        if should_stop is not None and "register_should_stop_callback" in params:
            agent_kwargs["register_should_stop_callback"] = should_stop

        agent = Agent(**agent_kwargs)
        meta = {
            "llm": llm,
            "browser_session": browser_session,
            "system_prompt": system_prompt,
            "task": task,
            "version": mods["version"],
            "max_steps": max_steps,
            "initial_actions": bool(initial_actions),
        }
        return agent, meta

    @staticmethod
    def _params_run(Agent) -> set:
        try:
            return set(inspect.signature(Agent.run).parameters)
        except (TypeError, ValueError):
            return set()

    # --------------------------------------------------------------------- run
    @staticmethod
    def run_agent(agent, max_steps: int, on_step_end=None):
        """Run the agent whether run() is sync or async."""
        run = agent.run
        params = AgentBuilder._params_run(type(agent))
        accepts_max_steps = "max_steps" in params
        accepts_on_step_end = "on_step_end" in params

        def _call():
            kwargs = {}
            if accepts_max_steps:
                kwargs["max_steps"] = max_steps
            if accepts_on_step_end and on_step_end is not None:
                kwargs["on_step_end"] = on_step_end
            return run(**kwargs) if kwargs else run()

        if inspect.iscoroutinefunction(run):
            try:
                return asyncio.run(_call())
            except RuntimeError:
                loop = asyncio.new_event_loop()
                try:
                    return loop.run_until_complete(_call())
                finally:
                    loop.close()
        result = _call()
        if inspect.isawaitable(result):
            return asyncio.run(result)
        return result
