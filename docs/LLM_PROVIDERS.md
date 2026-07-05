# LLM Providers

KAOS uses two model tiers:

- `standard`: higher quality reasoning, planning, tool-heavy work.
- `lite`: cheaper/faster parsing, summaries, routing, memory maintenance.

The default setup uses DeepSeek for both tiers, with the top-level
orchestrator on Codex CLI (see below):

```env
KAOS_STANDARD_PROVIDER_CHAIN=deepseek
KAOS_LITE_PROVIDER_CHAIN=deepseek

DEEPSEEK_API_KEY=sk-...
```

Provider chains are ordered fallback lists. If the first provider is missing,
fails, or enters cooldown, KAOS tries the next provider.

The top-level supervisor/orchestrator can be routed through a separate chain:

```env
KAOS_ORCHESTRATOR_PROVIDER_CHAIN=codex-cli
```

If this is unset, the orchestrator uses the `standard` chain.

## Built-In Presets

| Provider id | Adapter | Default model | Key env | Notes |
|-------------|---------|---------------|---------|-------|
| `deepseek` | DeepSeek SDK | `deepseek-chat` | `DEEPSEEK_API_KEY` | Default standard + lite model |
| `openai` | OpenAI-compatible | `gpt-4.1-mini` | `OPENAI_API_KEY` | Official OpenAI endpoint |
| `codex-cli` | Codex CLI | `gpt-5.5` | none | Uses local Codex OAuth/API login |
| `openrouter` | OpenAI-compatible | `openai/gpt-4.1-mini` | `OPENROUTER_API_KEY` | Route many hosted models |
| `groq` | OpenAI-compatible | `llama-3.3-70b-versatile` | `GROQ_API_KEY` | Fast hosted inference |
| `together` | OpenAI-compatible | `meta-llama/Llama-3.3-70B-Instruct-Turbo` | `TOGETHER_API_KEY` | Hosted open models |
| `litellm` | OpenAI-compatible | `gpt-4.1-mini` | `LITELLM_API_KEY` | Local/team proxy; key optional |
| `ollama` | OpenAI-compatible | `llama3.1` | `OLLAMA_API_KEY` | Local endpoint; key optional |
| `local` | OpenAI-compatible | `llama3.1` | `LOCAL_LLM_API_KEY` | Generic local endpoint; key optional |

## Copy-Paste Recipes

### OpenAI

```env
KAOS_STANDARD_PROVIDER_CHAIN=openai
KAOS_LITE_PROVIDER_CHAIN=openai
KAOS_PROVIDER_OPENAI_MODEL=gpt-4.1-mini
OPENAI_API_KEY=sk-proj-...
```

### OpenAI / ChatGPT OAuth via Codex CLI

Use this when you want the main supervisor/orchestrator to use the machine's
Codex CLI login instead of an OpenAI API key:

```env
KAOS_ORCHESTRATOR_PROVIDER_CHAIN=codex-cli
KAOS_CODEX_COMMAND=codex
KAOS_CODEX_MODEL=gpt-5.5
KAOS_CODEX_TIMEOUT_SECONDS=180
```

On the machine that runs KAOS:

```bash
codex login --device-auth
kaos doctor
```

`codex-cli` is best for the orchestrator because it can use ChatGPT/Codex OAuth
credentials while KAOS still executes its own tools. Keep `lite` tasks on
DeepSeek or another fast provider unless you explicitly want every background
cron/model call to spawn Codex CLI.

For image/OCR intake:

```env
# Default Telegram image/OCR intake uses Codex CLI OAuth credentials.
KAOS_VISION_PROVIDER=codex-cli
KAOS_VISION_MODEL=gpt-5.5
```

Telegram image messages use Codex CLI by default, so `OPENAI_API_KEY` is not
required for image/OCR intake if the machine has already run `codex login`.
Codex CLI accepts image paths and uses the locally stored OAuth/API credentials.
The vision path analyzes the image first, then passes the extracted
text/description into the normal KAOS dialogue flow.

If you want to bypass Codex CLI and call the OpenAI Responses API directly:

```env
KAOS_VISION_PROVIDER=openai-api
OPENAI_API_KEY=sk-proj-...
KAOS_VISION_MODEL=gpt-5.5
```

### OpenRouter

```env
KAOS_STANDARD_PROVIDER_CHAIN=openrouter,deepseek
KAOS_LITE_PROVIDER_CHAIN=deepseek,openrouter
OPENROUTER_API_KEY=sk-or-...
KAOS_PROVIDER_OPENROUTER_MODEL=openai/gpt-4.1-mini
```

### Groq

```env
KAOS_STANDARD_PROVIDER_CHAIN=groq,deepseek
KAOS_LITE_PROVIDER_CHAIN=groq
GROQ_API_KEY=gsk_...
KAOS_PROVIDER_GROQ_MODEL=llama-3.3-70b-versatile
```

### LiteLLM Proxy

```env
KAOS_STANDARD_PROVIDER_CHAIN=litellm
KAOS_LITE_PROVIDER_CHAIN=litellm
KAOS_PROVIDER_LITELLM_BASE_URL=http://127.0.0.1:4000/v1
KAOS_PROVIDER_LITELLM_MODEL=gpt-4.1-mini
KAOS_PROVIDER_LITELLM_API_KEY_ENV=LITELLM_API_KEY
LITELLM_API_KEY=sk-...
```

If your LiteLLM proxy does not require a key:

```env
KAOS_PROVIDER_LITELLM_API_KEY_REQUIRED=false
```

### Ollama / Local OpenAI-Compatible Endpoint

```env
KAOS_STANDARD_PROVIDER_CHAIN=ollama
KAOS_LITE_PROVIDER_CHAIN=ollama
KAOS_PROVIDER_OLLAMA_BASE_URL=http://127.0.0.1:11434/v1
KAOS_PROVIDER_OLLAMA_MODEL=llama3.1
KAOS_PROVIDER_OLLAMA_API_KEY_REQUIRED=false
```

### Arbitrary OpenAI-Compatible Provider

Use any provider id you want. KAOS normalizes dashes to underscores for env vars.

```env
KAOS_STANDARD_PROVIDER_CHAIN=my-lab
KAOS_LITE_PROVIDER_CHAIN=my-lab

KAOS_PROVIDER_MY_LAB_MODEL=my-model
KAOS_PROVIDER_MY_LAB_BASE_URL=https://llm.example.com/v1
KAOS_PROVIDER_MY_LAB_API_KEY_ENV=MY_LAB_API_KEY
MY_LAB_API_KEY=sk-...
KAOS_PROVIDER_MY_LAB_MAX_TOKENS=4096
KAOS_PROVIDER_MY_LAB_TEMPERATURE=0.5
```

## Compatibility Matrix

| Provider | Chat | Tool calling | Local | Recommended tier | Notes |
|----------|------|--------------|-------|------------------|-------|
| DeepSeek | yes | provider/model dependent | no | `standard`, `lite` | KAOS default standard + lite model |
| OpenAI | yes | strong | no | `standard` or `lite` | Most predictable for tools |
| Codex CLI | yes | KAOS JSON bridge | local CLI | `orchestrator` | Uses `codex login`; slower startup per call |
| OpenRouter | yes | model dependent | no | `standard` | Choose a model with tool support |
| Groq | yes | model dependent | no | `lite` or fast standard | Very fast, check tool support per model |
| Together | yes | model dependent | no | `standard` | Good hosted open-model option |
| LiteLLM | yes | upstream dependent | yes/proxy | both | Best team/proxy abstraction |
| Ollama/local | yes | model/server dependent | yes | `lite`, local testing | Great for private/local demos, weaker tools |

## Troubleshooting

- Run `kaos doctor` after editing `.env`; it prints the resolved standard/lite
  provider chains and the orchestrator chain when set.
- For `codex-cli`, `kaos doctor` must run under the same OS user that runs KAOS.
  Complete `codex login --device-auth` for that user.
- If a provider shows missing, check the chain id and key env var.
- If a provider is OpenAI-compatible but not listed above, configure it with
  `KAOS_PROVIDER_<ID>_MODEL`, `BASE_URL`, and `API_KEY_ENV`.
- If tool calling fails, try a model known to support tools or place a stronger
  provider first in the `standard` chain.
- Local providers often work for chat but may be weaker for structured tool
  calls, JSON-like output, and long context.

## Contributing Providers

Most providers should not need custom Python code. Prefer a preset PR when the
provider is OpenAI-compatible. Add:

- preset defaults in `kronos/llm.py`
- docs recipe in this file
- a provider config test in `tests/test_llm_providers.py`
- compatibility matrix row

Only add a new adapter when the provider is not OpenAI-compatible.
