# Agent Swarm (Local + OpenRouter) | OpenClaw Skill

> IMPORTANT: WEBGPU DAEMON + OPENROUTER REQUIRED
>
> Agent Swarm (Local + OpenRouter) uses local Ollama models via WebLLM/WebGPU daemon for orchestrator and simple tasks (persistent, zero-cost), and OpenRouter for complex tasks.
> Without WebGPU daemon running with persistent models, local routing will fall back to OpenRouter.

**Hybrid LLM routing and subagent delegation.** Routes simple tasks to local Ollama models (zero cost, persistent via WebGPU), complex tasks to OpenRouter cloud models. **Best of both worlds:** local efficiency + cloud power when needed. **Parallel tasks:** one message can spawn multiple subagents at once.

**v1.1.0 — Hybrid local + cloud routing with health checking.** Persistent local models via WebGPU daemon (no initialization overhead), intelligent routing to OpenRouter for complex tasks. **Source:** Based on agent-swarm skill, optimized for hybrid local/cloud usage.

Agent Swarm (Local + OpenRouter) routes your OpenClaw tasks intelligently: simple tasks run on local models (free, fast, persistent), complex tasks route to OpenRouter (powerful cloud models). You get zero-cost local processing for most tasks, with cloud power available when needed.

## Why Agent Swarm (Local + OpenRouter)

Perfect for users who want to minimize API costs while maintaining access to powerful cloud models:

- **Zero cost** for simple tasks (local models via WebGPU)
- **Persistent models** (no initialization overhead - models stay loaded in memory)
- **Fast responses** (local models, no network latency)
- **Smart routing** (complex tasks automatically use OpenRouter)
- **Cost efficiency** (only pay for cloud models when you need them)

## Requirements (critical)

**Platform Configuration Required:**
- **WebGPU Daemon**: Must be running with persistent models (Llama 3.2, Mistral, CodeLlama pre-loaded)
- **OpenRouter API key**: Must be configured in OpenClaw platform settings for complex tasks
- **OPENCLAW_HOME** (optional): Environment variable pointing to OpenClaw workspace root. If not set, defaults to `~/.openclaw`
- **openclaw.json access**: The router reads `tools.exec.host` and `tools.exec.node` from `openclaw.json` (located at `$OPENCLAW_HOME/openclaw.json` or `~/.openclaw/openclaw.json`). Only these two fields are accessed.

**Model Requirements:**
- **Local models**: Use `ollama/...` prefix (e.g. `ollama/llama3.2`)
- **Cloud models**: Use `openrouter/...` prefix
- **Persistent models**: Pre-loaded in WebGPU daemon (no initialization overhead)

## Default behavior

**Session default / orchestrator:** Llama 3.2 Local (`ollama/llama3.2`) — persistent, zero-cost, fast.

The router delegates tasks to tier-specific sub-agents:
- **Simple tasks** → Local models (Llama 3.2, Mistral) - FREE
- **Complex tasks** → OpenRouter models (GLM 4.7, Claude Sonnet 4) - Cloud

---

## Orchestrator flow (task delegation)

The **main agent (Llama 3.2 Local)** does not do user tasks itself. For every user **task**:

1. Run Agent Swarm router: `python scripts/router.py spawn --json "<user message>"` and parse the JSON.
2. Router intelligently selects local (free) or cloud (powerful) model based on task complexity.
3. Call **sessions_spawn** with the `task` and `model` from the router output (use the exact `model` value).
4. Forward the sub-agent's result to the user.

**Example:**
```
Simple task: "check server status"
→ router: {"model": "ollama/llama3.2", ...}  # Local, FREE
→ sessions_spawn(task="check server status", model="ollama/llama3.2", ...)

Complex task: "design microservices architecture"
→ router: {"model": "openrouter/z-ai/glm-4.7", ...}  # Cloud, powerful
→ sessions_spawn(task="design microservices architecture", model="openrouter/z-ai/glm-4.7", ...)
```

**Exception:** Meta-questions ("what model are you?") you answer yourself.

### Parallel tasks

For one message with multiple tasks, use **`spawn --json --multi "<message>"`**. The router splits on *and*, *then*, *;*, and *also*, classifies each part, and routes simple tasks to local models and complex tasks to OpenRouter.

**Example:** `spawn --json --multi "check status and design architecture"` → two spawns (local Llama 3.2 for status check, OpenRouter GLM 4.7 for architecture).

---

## Quick start

### 1. Start WebGPU Daemon

```bash
# Start daemon with persistent models
webgpu-daemon --port 8080 --persistent --preload llama3.2 mistral codellama

# Or configure via environment
export WEBGPU_DAEMON_PORT=8080
export WEBGPU_PERSISTENT=true
export WEBGPU_PRELOAD_MODELS="llama3.2,mistral,codellama"
```

### 2. Install the Skill

```bash
cd ~/.openclaw/workspace/skills
git clone <repository-url> agent-swarm-local

python scripts/router.py default
python scripts/router.py classify "your task description"
```

---

## Features

- **Orchestrator** — Llama 3.2 Local (persistent, zero-cost) delegates to tier-specific sub-agents
- **Hybrid routing** — Simple tasks → local (free), complex tasks → OpenRouter (powerful)
- **Persistent models** — Models stay loaded in WebGPU daemon (no initialization overhead)
- **Parallel tasks** — `spawn --json --multi "task A and task B"` splits and routes intelligently
- 7 tiers: FAST, REASONING, CREATIVE, RESEARCH, CODE, QUALITY, VISION
- **Security-focused** — Input validation, config patch whitelisting

---

## Default agents (edit in config.json)

All defaults are in **`config.json`**. Edit these to change routing:

| What to edit | Key in config.json | Purpose |
|--------------|--------------------|---------|
| Session default / orchestrator | `default_model` | Model for new sessions and the main agent (local) |
| Per-tier primary | `routing_rules.<TIER>.primary` | Model used when a task matches that tier |
| Prefer local | `routing_rules.<TIER>.prefer_local` | Whether to prefer local models for this tier |

Example: to make CODE prefer local models, set `routing_rules.CODE.prefer_local` to `true`.

---

## Models

### Local Models (FREE, Persistent)

| Tier | Model | Cost | Status |
|------|-------|------|--------|
| **Default / orchestrator** | Llama 3.2 Local | FREE | Persistent |
| FAST | Llama 3.2 Local | FREE | Persistent |
| RESEARCH | Llama 3.2 Local | FREE | Persistent |
| CODE | Mistral Local | FREE | Persistent |
| CODE | CodeLlama Local | FREE | Persistent |

### Cloud Models (OpenRouter)

| Tier | Model | Cost/M (in/out) |
|------|-------|-----------------|
| FAST (fallback) | Gemini 2.5 Flash | $0.30 / $2.50 |
| REASONING | GLM-5 | $0.10 / $0.10 |
| CREATIVE | Kimi k2.5 | $0.20 / $0.20 |
| CODE (fallback) | GLM 4.7 Flash | $0.06 / $0.40 |
| QUALITY | GLM 4.7 | $0.40 / $1.50 |
| QUALITY | Claude Sonnet 4 | $3.00 / $15.00 |
| VISION | GPT-4o | $2.50 / $10.00 |

**Fallbacks:** Local models fall back to OpenRouter if unavailable. Complex tasks prefer OpenRouter.

---

## CLI usage

```bash
python scripts/router.py default                          # Show default model (local)
python scripts/router.py classify "fix lint errors"        # Classify → tier + model (local or cloud)
python scripts/router.py score "build a React auth system" # Detailed scoring
python scripts/router.py cost "design a landing page"      # Cost estimate (FREE if local)
python scripts/router.py spawn "research best LLMs"        # Spawn params (human)
python scripts/router.py spawn --json "research best LLMs" # JSON for sessions_spawn
python scripts/router.py spawn --json --multi "fix bug and write poem" # Parallel tasks
python scripts/router.py models                            # List all models (local + cloud)
```

---

## WebGPU Daemon Configuration

### Persistent Models

Models are pre-loaded in the WebGPU daemon and stay in memory:

```json
{
  "local_daemon": {
    "type": "webgpu",
    "persistent": true,
    "models_preload": ["ollama/llama3.2", "ollama/mistral"],
    "webllm_config": {
      "cache_dir": "~/.webllm_cache",
      "model_lib_dir": "~/.webllm_model_lib",
      "use_ndarray_cache": true
    }
  }
}
```

### Starting the Daemon

```bash
# Basic start
webgpu-daemon --port 8080

# With persistent models (recommended)
webgpu-daemon --port 8080 --persistent --preload llama3.2 mistral codellama

# With custom cache directory
webgpu-daemon --port 8080 --persistent --preload llama3.2 --cache-dir ~/.webllm_cache
```

### Environment Variables

```bash
export WEBGPU_DAEMON_PORT=8080
export WEBGPU_PERSISTENT=true
export WEBGPU_PRELOAD_MODELS="llama3.2,mistral,codellama"
export WEBLLM_CACHE_DIR=~/.webllm_cache
export WEBLLM_MODEL_LIB_DIR=~/.webllm_model_lib
```

### Model Availability Check

The router checks if local models are available before routing:

```python
# Router automatically checks WebGPU daemon
if check_local_model_available("ollama/llama3.2"):
    # Route to local model (FREE)
else:
    # Fall back to OpenRouter
```

---

## Tier detection

| Tier | Example keywords | Routing |
|------|------------------|---------|
| **FAST** | check, get, list, show, status, monitor, fetch, simple | **Local** (Llama 3.2) |
| **REASONING** | prove, logic, analyze, derive, math, step by step | **Cloud** (GLM-5) |
| **CREATIVE** | creative, write, story, design, UI, UX, frontend | **Cloud** (Kimi k2.5) |
| **RESEARCH** | research, find, search, lookup, web, information | **Local** (Llama 3.2) |
| **CODE** | code, function, debug, fix, implement, refactor, test | **Local** (Mistral/CodeLlama) |
| **QUALITY** | complex, architecture, design, system, comprehensive | **Cloud** (GLM 4.7 / Claude Sonnet 4) |
| **VISION** | image, picture, photo, screenshot, visual | **Cloud** (GPT-4o) |

- **Local models** are used for FAST, RESEARCH, and CODE tasks (zero cost)
- **Cloud models** are used for REASONING, CREATIVE, QUALITY, and VISION tasks
- **Fallback**: If local models unavailable, router falls back to OpenRouter

---

## Cost Savings

### Example: 1,000 Tasks/Month

| Task Type | Count | Local Cost | Cloud Cost (if all cloud) | Savings |
|-----------|-------|------------|---------------------------|---------|
| FAST | 400 | **$0** | $9.00 | **$9.00** |
| RESEARCH | 150 | **$0** | $7.88 | **$7.88** |
| CODE | 250 | **$0** | $35.63 | **$35.63** |
| CREATIVE | 100 | $3.45 | $17.25 | $13.80 |
| QUALITY | 80 | $4.80 | $24.00 | $19.20 |
| COMPLEX | 20 | $9.90 | $9.90 | $0.00 |
| **TOTAL** | **1,000** | **$18.15** | **$103.66** | **$85.51 (82.5% savings)** |

**Key Insight:** Simple tasks (FAST, RESEARCH, CODE) represent 80% of workload but cost $0 with local models vs $52.51 with cloud-only.

---

## Security

### Input Validation

The router validates and sanitizes all inputs to prevent injection attacks:

- **Task strings**: Validated for length (max 10KB), null bytes, and suspicious patterns
- **Config patches**: Only allows modifications to `tools.exec.host` and `tools.exec.node` (whitelist approach)
- **Labels**: Validated for length and null bytes

### Safe Execution (Critical for Orchestrators)

**When calling `router.py` from orchestrator code, always use `subprocess` with a list of arguments, never shell string interpolation:**

```python
# ✅ SAFE: Use subprocess with list arguments
import subprocess
result = subprocess.run(
    ["python3", "/path/to/router.py", "spawn", "--json", user_message],
    capture_output=True,
    text=True
)

# ❌ UNSAFE: Shell string interpolation (vulnerable to injection)
import os
os.system(f'python3 router.py spawn --json "{user_message}"')  # DON'T DO THIS
```

### Config Patch Safety

The `recommended_config_patch` only modifies safe fields:
- `tools.exec.host` (must be 'sandbox' or 'node')
- `tools.exec.node` (only when host is 'node')

### File Access Scope

The router reads `openclaw.json` **only** to inspect `tools.exec.host` and `tools.exec.node` configuration. This is necessary to determine the execution environment for spawned sub-agents.

**Important:**
- The router **does not** read gateway secrets, API keys, or any other sensitive configuration
- Only `tools.exec.host` and `tools.exec.node` are accessed
- No data is written to `openclaw.json` except via validated config patches (whitelisted to `tools.exec.*` only)
- The router does not persist, upload, or transmit any tokens or credentials

---

## Configuration

- **`config.json`** — Model list and `routing_rules` per tier; `default_model` (e.g. `ollama/llama3.2`) for session default and orchestrator; `local_daemon` config for WebGPU.
- Router loads `config.json` from the parent of `scripts/` (skill root).

---

## Local Model Health Check

The router can verify local model availability and response quality before routing tasks:

```bash
# Check all local models
python scripts/router.py health

# JSON output
python scripts/router.py health --json
```

Health check output:
```json
{
  "daemon_running": true,
  "daemon_url": "http://localhost:8080",
  "models": {
    "ollama/llama3.2": {"available": true, "response_ms": 45, "quality": "ok"},
    "ollama/mistral": {"available": true, "response_ms": 62, "quality": "ok"},
    "ollama/codellama": {"available": false, "error": "model not loaded"}
  },
  "recommendation": "2/3 local models available. codellama tasks will fallback to OpenRouter."
}
```

The router automatically falls back to OpenRouter if a local model is unavailable or returns low-quality responses.

---

## WebGPU Daemon Deep Dive

### Architecture

The WebGPU daemon acts as a persistent model server:

```
┌─────────────────────────────────────────────┐
│  Agent Swarm Router                          │
│  ├── classify_task() → tier                  │
│  ├── check_local_model_available()           │
│  │   └── GET http://localhost:8080/health     │
│  └── spawn_agent() → {model, task, ...}      │
└─────────┬───────────────────────┬────────────┘
          │ local tasks           │ complex tasks
          ▼                       ▼
┌─────────────────┐    ┌──────────────────┐
│  WebGPU Daemon   │    │  OpenRouter API   │
│  port 8080       │    │  (cloud models)   │
│  ├── llama3.2    │    │  ├── GLM 4.7      │
│  ├── mistral     │    │  ├── Kimi k2.5    │
│  └── codellama   │    │  ├── GPT-4o       │
│  (persistent)    │    │  └── Claude S4    │
└─────────────────┘    └──────────────────┘
```

### Daemon Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Daemon status and uptime |
| `/models` | GET | List loaded models with status |
| `/v1/chat/completions` | POST | OpenAI-compatible chat API |
| `/v1/models` | GET | OpenAI-compatible model list |

### Configuration File

The daemon reads configuration from `config.json` under `local_daemon`:

```json
{
  "local_daemon": {
    "type": "webgpu",
    "persistent": true,
    "models_preload": ["ollama/llama3.2", "ollama/mistral", "ollama/codellama"],
    "webllm_config": {
      "cache_dir": "~/.webllm_cache",
      "model_lib_dir": "~/.webllm_model_lib",
      "use_ndarray_cache": true
    }
  }
}
```

### Memory Management

- Models are loaded once and stay in GPU memory
- Typical memory usage: ~2-4 GB per model depending on size
- Cache directory stores model weights for fast reload after daemon restart
- Use `--preload` to specify which models to load at startup

### Troubleshooting Daemon Issues

| Symptom | Cause | Fix |
|---------|-------|-----|
| Daemon won't start | Port in use | Change `WEBGPU_DAEMON_PORT` or kill existing process |
| Models load slowly | No cache | Run once with `--persistent` to cache; subsequent starts are fast |
| Out of memory | Too many models | Reduce `--preload` list or use smaller model variants |
| GPU not detected | Missing drivers | Install WebGPU-compatible GPU drivers |

---

## Troubleshooting

### Local Models Not Available

If local models aren't available, the router automatically falls back to OpenRouter:

```bash
# Check WebGPU daemon status
curl http://localhost:8080/health

# Check loaded models
curl http://localhost:8080/models

# Restart daemon with persistent models
webgpu-daemon --port 8080 --persistent --preload llama3.2 mistral codellama
```

### Model Initialization Slow

If models are slow to initialize, ensure they're pre-loaded:

```bash
# Pre-load models on daemon start
webgpu-daemon --port 8080 --persistent --preload llama3.2 mistral codellama
```

Models stay loaded in memory - no initialization overhead on subsequent requests.

---

## License / author

Austin. Part of the OpenClaw skills ecosystem.
