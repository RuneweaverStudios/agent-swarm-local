---
name: agent-swarm-local
displayName: Agent Swarm (Local + OpenRouter) | OpenClaw Skill
description: "Hybrid routing: local Ollama models via WebLLM/WebGPU (persistent, zero-cost) for orchestrator and simple tasks, OpenRouter for complex tasks."
version: 1.1.0
---

# Agent Swarm (Local + OpenRouter) | OpenClaw Skill

## What this skill does

Agent Swarm (Local + OpenRouter) is a hybrid traffic cop for AI models.
It uses **local Ollama models via WebLLM/WebGPU** for the orchestrator and simple tasks (zero cost, persistent, no initialization overhead), and routes complex tasks to **OpenRouter** (cloud models).

### IMPORTANT: WebGPU Daemon + OpenRouter Required

**Required Platform Configuration:**
- **WebGPU Daemon**: Must be running with persistent models (Llama 3.2, Mistral, CodeLlama)
- **OpenRouter API key**: Must be configured in OpenClaw platform settings for complex tasks
- **OPENCLAW_HOME** (optional): Environment variable pointing to OpenClaw workspace root. If not set, defaults to `~/.openclaw`
- **openclaw.json access**: The router reads `tools.exec.host` and `tools.exec.node` from `openclaw.json`. Only these two fields are accessed.

**Model Requirements:**
- Local models use `ollama/...` prefix (e.g. `ollama/llama3.2`)
- Cloud models use `openrouter/...` prefix
- Local models are persistent (pre-loaded in WebGPU daemon, no initialization overhead)

## Why this helps

- **Zero cost** for simple tasks (local models via WebGPU)
- **Faster responses** (local models, no network latency)
- **Persistent models** (no initialization overhead - models stay loaded)
- **Smart fallback** (complex tasks automatically route to OpenRouter)
- **Best of both worlds**: Local efficiency + cloud power when needed

## Core rule (non-negotiable)

For user tasks, the orchestrator must delegate.
It must NOT answer the task itself.

Use this flow every time:

1. Run router:
   ```bash
   python3 workspace/skills/agent-swarm-local/scripts/router.py spawn --json "<user message>"
   ```
   
   **Note:** Use relative paths from your OpenClaw workspace root, or set `OPENCLAW_HOME` environment variable.
2. If `needs_config_patch` is true: stop and report that patch to the user.
3. Otherwise call:
   `sessions_spawn(task=..., model=..., sessionTarget=...)`
4. Wait for `sessions_spawn` result.
5. Return the sub-agent result to the user.

If `sessions_spawn` fails, return only a delegation failure message.
Do not do the task yourself.

## Quick examples

### Simple task (routes to local model)

Router output:
`{"task":"check server status","model":"ollama/llama3.2","sessionTarget":"isolated"}`

Then call:
`sessions_spawn(task="check server status", model="ollama/llama3.2", sessionTarget="isolated")`

### Complex task (routes to OpenRouter)

Router output:
`{"task":"design a microservices architecture","model":"openrouter/z-ai/glm-4.7","sessionTarget":"isolated"}`

Then call:
`sessions_spawn(task="design a microservices architecture", model="openrouter/z-ai/glm-4.7", sessionTarget="isolated")`

### Parallel tasks

```bash
python3 workspace/skills/agent-swarm-local/scripts/router.py spawn --json --multi "check status and design architecture"
```

This routes simple tasks to local models and complex tasks to OpenRouter.

## Commands

```bash
python scripts/router.py default
python scripts/router.py classify "fix lint errors"
python scripts/router.py spawn --json "write a poem"
python scripts/router.py spawn --json --multi "fix bug and write poem"
python scripts/router.py models
```

## Config basics

Edit `config.json` to change routing:

- `default_model` = orchestrator default (local Llama 3.2)
- `routing_rules.<TIER>.primary` = main model for tier
- `routing_rules.<TIER>.prefer_local` = whether to prefer local models
- `routing_rules.<TIER>.fallback` = backups

## WebGPU Daemon Setup

### Persistent Models

The WebGPU daemon keeps models loaded in memory, eliminating initialization overhead:

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
# Start WebGPU daemon with persistent models
webgpu-daemon --port 8080 --persistent --preload llama3.2 mistral codellama
```

Models stay loaded and ready - no initialization delay on each request.

## Security

### Input Validation

The router validates and sanitizes all inputs to prevent injection attacks:

- **Task strings**: Validated for length (max 10KB), null bytes, and suspicious patterns
- **Config patches**: Only allows modifications to `tools.exec.host` and `tools.exec.node` (whitelist approach)
- **Labels**: Validated for length and null bytes

### Safe Execution

**Critical**: When calling `router.py` from orchestrator code, always use `subprocess` with a list of arguments, **never** shell string interpolation:

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

**Required File Access:**
- **Read**: `openclaw.json` (located via `OPENCLAW_HOME` environment variable or `~/.openclaw/openclaw.json`)
  - **Fields accessed**: `tools.exec.host` and `tools.exec.node` only
  - **Purpose**: Determine execution environment for spawned sub-agents
  - **Security**: The router does NOT read gateway secrets, API keys, or any other sensitive configuration

**Write Access:**
- **Write**: None (no files are written by this skill)
- **Config patches**: The skill may return `recommended_config_patch` JSON that the orchestrator can apply, but the skill itself does not write to `openclaw.json`

**Security Guarantees:**
- The router does not persist, upload, or transmit any tokens or credentials
- Only `tools.exec.host` and `tools.exec.node` are accessed from `openclaw.json`
- All file access is read-only except for validated config patches (whitelisted to `tools.exec.*` only)

### Other Security Notes

- This skill does not expose gateway secrets.
- Use `gateway-guard` separately for gateway/auth management.
- The router does not execute arbitrary code or modify files outside of config patches.
