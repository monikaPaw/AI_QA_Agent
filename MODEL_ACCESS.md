# Model Access Configuration

Edit this file to use any LLM provider. The runner reads this and configures Browser Use.

## Active Configuration
# --- Google Gemini (free tier) ---
- Provider: gemini 
- Model: gemini-3.5-flash-lite
- Base URL: https://generativelanguage.googleapis.com/v1beta/openai/ 
- API Key:  # from GEMINI_API_KEY / GOOGLE_API_KEY in .env (see Tips)
- Vision: true  # Gemini is multimodal; enables screenshot-based anomaly checks

# --- Local Ollama (commented out) ---
 #- Provider: custom  # 'custom' = any OpenAI-compatible endpoint; Ollama's /v1 API
 #- Model: qwen2.5-1.5b-8k  # local model (see Tips at the bottom)
 #- Base URL: http://localhost:11434/v1  # local Ollama
 #- API Key: ollama  # Ollama ignores this value; it only has to be non-empty

# --- OpenRouter (commented out) ---
#- Provider: openrouter
#- Model: nvidia/nemotron-3.5-super-vl:free 
#- Base URL: https://openrouter.ai/api/v1
#- API Key:  # from OPENROUTER_API_KEY in .env (see Tips)
#- Vision: true  # is multimodal; enables screenshot-based anomaly checks 

- Temperature: 0.1  # Lower = more deterministic, better for QA
- Max Tokens: 8192  # Caps ONE response. 4096 truncated the model's structured output mid-run; 8192 removes that failure.
- Max Steps: 35  # Max agent steps. With one action per step, a full checkout + verification needs ~15-20; 35 leaves retry headroom
- LLM Timeout: 180  # Seconds to wait for one model response. Free/slow models often exceed 60s; keep this BELOW Step Timeout
- Add Schema To Prompt: false  # Gemini uses its native structured output; leaving this true reroutes to ChatOpenAI (which is what broke on the compat endpoint)
- LLM Max Retries: 2  # Retries per model call (a 429 would otherwise stall the run)

<!-- Speed / Reliability Profile (AI agent path). These stay inside the Active
     Configuration section on purpose so the loader picks them up. They are what
     make a run finish in a couple of minutes instead of looking stuck. Only turn
     them back on if you want the agent's full deliberation. -->
- Use Judge: false  # TRUE adds an EXTRA LLM call after every step (roughly doubles runtime)
- Enable Planning: false  # TRUE lets the agent re-plan mid-run (more LLM calls)
- Flash Mode: true  # action-only output: much shorter prompts and responses
- Use Thinking: false  # TRUE writes long reasoning blocks every step (slow on free models)
- Max Actions Per Step: 1  # one action per step: a bad action can no longer abort a whole batch, and the agent sees each result
- Max Failures: 2  # stop retrying a broken action after 2 tries instead of 5
- Step Timeout: 240  # outer per-step cap; MUST be greater than LLM Timeout or the step is killed before the model answers
- Loop Detection: true  # bail out when the agent repeats itself
- Loop Detection Window: 8  # how many recent actions to compare (browser-use default: 20)
- Wait Between Actions: 0.1  # default 0.1s of pure waiting per action
- Min Page Load Wait: 0.25  # default 0.25
- Network Idle Wait: 0.5  # default 0.5
- Demo Budget Seconds: 1200  # hard wall-clock cap; the run always ends and still writes a report. Slow free models need ~15-20 min

## Browser Use Settings
- Headless: false  # Set to true to run without UI (headless)
- Browser: chromium  # chromium | firefox | webkit
- Save Screenshots: true
- Save Agent History: true

## Provider Examples

### OpenRouter (Free-ish credits often available, flexible)
- Provider: openrouter
- Model: qwen/qwen2.5-vl-72b-instruct  # or google/gemini-2.5-flash-preview, anthropic/claude-sonnet-4.5
- Base URL: https://openrouter.ai/api/v1
- API Key: sk-or-v1-...

### OpenAI (Direct)
- Provider: openai
- Model: gpt-4o  # or gpt-4.1, o3-mini
- Base URL: https://api.openai.com/v1
- API Key: sk-proj-...

### Anthropic
- Provider: anthropic
- Model: claude-sonnet-4-5  # check current name
- Base URL: https://api.anthropic.com/v1
- API Key: sk-ant-...

### Gemini
- Provider: gemini
- Model: gemini-2.5-flash  # or gemini-2.5-pro
- Base URL: https://generativelanguage.googleapis.com/v1beta/openai/  # use OpenAI-compatible if preferred

### Groq (Very fast, often cheap)
- Provider: groq
- Model: meta-llama/llama-3.2-90b-vision-instruct  # or qwen/qwen2.5-vl-32b-instruct
- Base URL: https://api.groq.com/openai/v1
- API Key: gsk_...

### Cerebras (Very fast inference)
- Provider: cerebras
- Model: llama-3.1-70b  # or qwen2.5-vl variants as available
- Base URL: https://api.cerebras.ai/v1
- API Key: csk-...

### Custom/OpenAI-Compatible
- Provider: custom
- Model: your-model-name
- Base URL: https://your-endpoint.example.com/v1
- API Key: your-key

## Tips
- On the OpenRouter **free tier** (no credits) you must use a `:free` model, e.g.
  `google/gemma-4-31b-it:free`, `nvidia/nemotron-3-super-120b-a12b:free`,
  `dots-studio/dots-3-note-preview:free`. Free models allow ~50 requests/day, so keep `Max Steps` modest.
- Prefer models that support **tools + structured output** (required by Browser Use). You can check this on
  the OpenRouter models page under "Supported parameters".
- Prefer **vision-capable** models (VL) to detect visual anomalies like identical product images.
- For local Ollama: set `Provider: custom`, `Base URL: http://localhost:11434/v1`,
  and a `Model` you have pulled (e.g. `qwen2.5:3b`). The API key can be any non-empty
  string. Pick a model small enough to fit RAM: a 3B Q4 model needs ~2-3 GB, a 7B model
  needs ~5 GB+ and will be very slow (CPU inference) on a low-RAM laptop.
  Keep `Add Schema To Prompt: true` — small local models rarely support JSON schemas.
- You can also set environment variables (OPENROUTER_API_KEY, OPENAI_API_KEY etc.) and leave API Key empty here.
  The project-local `.env` file (gitignored) is loaded automatically; real environment variables take precedence
  over it, so CI can inject keys without touching the file. See `.env.example` for the expected names.
- Set Headless=true if running in CI or don't want browser window.
- Never commit real API keys. Add MODEL_ACCESS.md to .gitignore if you put keys in it.
