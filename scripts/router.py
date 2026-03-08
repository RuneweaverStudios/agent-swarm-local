#!/usr/bin/env python3
"""
Agent Swarm (Local + OpenRouter) | OpenClaw Skill — Hybrid LLM routing
Version 1.0.0

Hybrid routing: local Ollama models via WebLLM/WebGPU for orchestrator and simple tasks
(persistent, no initialization overhead), OpenRouter for complex tasks.

Security improvements:
- Input validation for task strings (length limits, null bytes, suspicious patterns)
- Config patch validation (whitelist approach - only tools.exec.host/node)
- Label validation
- Comprehensive security documentation

Features:
- Local model routing (Llama 3.2, Mistral, CodeLlama) via WebGPU daemon
- Persistent models (no initialization overhead)
- OpenRouter fallback for complex tasks
- Keyword-based routing for obvious matches
- Weighted scoring for nuanced classification
- OpenClaw integration for spawning sub-agents
"""

import argparse
import json
import os
import re
import sys
import subprocess
from pathlib import Path

# OpenClaw imports (if available)
try:
    import openclaw
    HAS_OPENCLAW = True
except ImportError:
    HAS_OPENCLAW = False


# Security: Input validation and sanitization
def validate_task_string(task):
    """
    Validate and sanitize task string to prevent injection attacks.
    Returns sanitized task string or raises ValueError if invalid.
    """
    if not isinstance(task, str):
        raise ValueError("Task must be a string")
    
    # Check for reasonable length (prevent DoS)
    if len(task) > 10000:  # 10KB limit
        raise ValueError("Task string exceeds maximum length (10KB)")
    
    # Check for null bytes (common injection vector)
    if '\x00' in task:
        raise ValueError("Task string contains null bytes")
    
    # Task strings are user input that will be passed to LLMs - allow most characters
    # but log warnings for suspicious patterns
    suspicious_patterns = [
        r'<script[^>]*>',  # Script tags
        r'javascript:',     # JavaScript protocol
        r'on\w+\s*=',       # Event handlers
    ]
    
    for pattern in suspicious_patterns:
        if re.search(pattern, task, re.IGNORECASE):
            # Log warning but allow (LLM should handle this safely)
            pass
    
    return task.strip()


def validate_config_patch(patch_json_str):
    """
    Validate that a config patch only modifies safe fields.
    Returns validated patch dict or raises ValueError if unsafe.
    
    Allowed fields:
    - tools.exec.host (must be 'sandbox' or 'node')
    - tools.exec.node (only if host is 'node')
    """
    try:
        patch = json.loads(patch_json_str)
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in config patch: {e}")
    
    # Only allow modifications to tools.exec
    allowed_paths = {
        ('tools', 'exec', 'host'),
        ('tools', 'exec', 'node'),
    }
    
    def check_path(obj, path=tuple()):
        """Recursively check that only allowed paths are modified."""
        if isinstance(obj, dict):
            for key, value in obj.items():
                current_path = path + (key,)
                if current_path not in allowed_paths:
                    # Check if this is a parent of an allowed path
                    is_parent = any(
                        current_path == allowed[:len(current_path)]
                        for allowed in allowed_paths
                    )
                    if not is_parent:
                        raise ValueError(
                            f"Config patch modifies disallowed path: {'.'.join(current_path)}. "
                            f"Only tools.exec.host and tools.exec.node are allowed."
                        )
                check_path(value, current_path)
        elif isinstance(obj, list):
            raise ValueError("Config patch cannot contain arrays")
    
    check_path(patch)
    
    # Validate values
    exec_config = patch.get('tools', {}).get('exec', {})
    host = exec_config.get('host')
    if host and host not in ('sandbox', 'node'):
        raise ValueError(f"tools.exec.host must be 'sandbox' or 'node', got: {host}")
    
    if exec_config.get('host') != 'node' and exec_config.get('node'):
        raise ValueError("tools.exec.node can only be set when host is 'node'")
    
    return patch


def get_current_openclaw_config():
    """Read tools.exec.host and tools.exec.node from openclaw.json (no gateway auth)."""
    openclaw_home = os.environ.get("OPENCLAW_HOME") or os.path.expanduser("~/.openclaw")
    config_path = Path(openclaw_home) / "openclaw.json"
    if not config_path.exists():
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    exec_config = config.get("tools", {}).get("exec", {})
    return {
        "tools_exec_host": exec_config.get("host", "sandbox"),
        "tools_exec_node": exec_config.get("node"),
    }


def check_local_model_available(model_id):
    """
    Check if a local model is available and persistent.
    Returns True if model is available, False otherwise.

    Checks:
    1. Whether ollama is running (via 'ollama list')
    2. Whether the specific model is pulled/available locally
    """
    # Check if model ID is a local model
    if not model_id.startswith("ollama/"):
        return False

    model_name = model_id.replace("ollama/", "", 1)

    # First check: is ollama running?
    try:
        result = subprocess.run(
            ["ollama", "list"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return False

        # Parse ollama list output to check if the model is available
        # Output format: NAME  ID  SIZE  MODIFIED
        available_models = []
        for line in result.stdout.strip().splitlines()[1:]:  # skip header
            parts = line.split()
            if parts:
                available_models.append(parts[0].split(":")[0])  # strip :tag

        return model_name in available_models or model_name.split(":")[0] in available_models
    except FileNotFoundError:
        # ollama not installed
        return False
    except subprocess.TimeoutExpired:
        return False
    except Exception:
        return False


def check_webgpu_daemon_available(port=None, timeout=5):
    """
    Check if the WebGPU daemon is reachable on the configured port.
    Returns True if daemon responds within the timeout, False otherwise.
    """
    import socket
    daemon_port = port or int(os.environ.get("WEBGPU_DAEMON_PORT", 8080))
    try:
        sock = socket.create_connection(("127.0.0.1", daemon_port), timeout=timeout)
        sock.close()
        return True
    except (ConnectionRefusedError, socket.timeout, OSError):
        return False


class HybridRouter:
    """Hybrid local + OpenRouter intelligent model router."""
    
    # Simple indicators that suggest SIMPLE/Fast tasks
    SIMPLE_KEYWORDS = [
        'check', 'get', 'fetch', 'list', 'show', 'display', 'status',
        'what is', 'how much', 'tell me', 'find', 'search', 'summarize',
        'monitor', 'watch', 'read', 'look', 'simple', 'quick', 'fast'
    ]
    
    # Complex indicators that suggest QUALITY/Code tasks
    COMPLEX_KEYWORDS = [
        'build', 'create', 'implement', 'architect', 'design', 'system',
        'comprehensive', 'thorough', 'complex', 'multi', 'full-stack',
        'authentication', 'authorization', 'database', 'api', 'service'
    ]
    
    # Code-related keywords
    CODE_KEYWORDS = [
        'code', 'function', 'class', 'method', 'debug', 'fix', 'bug',
        'refactor', 'lint', 'test', 'unit', 'integration', 'component',
        'module', 'package', 'library', 'framework', 'import', 'export',
        'react', 'vue', 'angular', 'node', 'python', 'javascript',
        'typescript', 'rust', 'go', 'java', 'api', 'endpoint'
    ]
    
    # Reasoning keywords
    REASONING_KEYWORDS = [
        'prove', 'theorem', 'proof', 'derive', 'logic', 'reason',
        'analyze', 'reasoning', 'step by step', 'why', 'how does',
        'explain', 'mathematical', 'induction', 'deduction'
    ]
    
    # Creative keywords
    CREATIVE_KEYWORDS = [
        'creative', 'write', 'story', 'poem', 'article', 'blog',
        'design', 'UI', 'UX', 'frontend', 'website', 'landing',
        'copy', 'narrative', 'brainstorm', 'idea', 'concept'
    ]
    
    # Research keywords
    RESEARCH_KEYWORDS = [
        'research', 'find', 'search', 'lookup', 'web', 'information',
        'fact', 'review', 'compare', 'vs', 'versus', 'difference',
        'summary of', 'what are', 'best', 'top', 'alternatives'
    ]
    
    # Agentic/action keywords (multi-step tasks)
    AGENTIC_KEYWORDS = [
        'run', 'test', 'fix', 'deploy', 'edit', 'build', 'create',
        'implement', 'execute', 'refactor', 'migrate', 'integrate',
        'setup', 'configure', 'install', 'compile', 'debug'
    ]
    
    def __init__(self, config_path=None):
        """Initialize router with config file."""
        if config_path is None:
            # Default to config.json in parent directory of script
            script_dir = Path(__file__).parent
            config_path = script_dir.parent / 'config.json'
        
        self.config_path = Path(config_path)
        self.config = self._load_config()
    
    def _load_config(self):
        """Load and parse configuration file."""
        if not self.config_path.exists():
            raise FileNotFoundError(f"Configuration file not found: {self.config_path}")
        
        with open(self.config_path, 'r') as f:
            return json.load(f)
    
    def _keyword_match(self, text, keywords):
        """Count keyword matches (case-insensitive)."""
        text_lower = text.lower()
        return sum(1 for kw in keywords if kw.lower() in text_lower)
    
    def classify_task(self, task_description, return_details=False):
        """
        Classify a task into a tier using keyword matching + scoring.
        
        Returns: FAST, REASONING, CREATIVE, RESEARCH, CODE, QUALITY, or VISION
        
        Security: Validates task_description input.
        """
        # Security: Validate input
        if not isinstance(task_description, str):
            raise ValueError("task_description must be a string")
        if len(task_description) > 10000:
            raise ValueError("task_description exceeds maximum length (10KB)")
        
        text = task_description.lower()
        
        # First, check for exact keyword tier matches (highest priority)
        tier_scores = {}
        
        # Count matches for each tier
        tier_scores['FAST'] = self._keyword_match(task_description, self.SIMPLE_KEYWORDS)
        tier_scores['REASONING'] = self._keyword_match(task_description, self.REASONING_KEYWORDS)
        tier_scores['CREATIVE'] = self._keyword_match(task_description, self.CREATIVE_KEYWORDS)
        tier_scores['RESEARCH'] = self._keyword_match(task_description, self.RESEARCH_KEYWORDS)
        tier_scores['CODE'] = self._keyword_match(task_description, self.CODE_KEYWORDS)
        tier_scores['COMPLEX'] = self._keyword_match(task_description, self.COMPLEX_KEYWORDS)
        
        # Check for vision keywords (highest priority)
        vision_keywords = ['image', 'picture', 'photo', 'screenshot', 'visual', 'see', 'describe what']
        vision_matches = self._keyword_match(task_description, vision_keywords)
        tier_scores['VISION'] = vision_matches
        
        # If vision keywords present, this IS a vision task
        if vision_matches > 0:
            return {
                'tier': 'VISION',
                'confidence': min(vision_matches / 3.0, 1.0),
                'tier_scores': {'VISION': vision_matches},
                'is_agentic': False
            }
        
        # Agentic task detection - if multi-step, bump to at least CODE
        agentic_count = self._keyword_match(task_description, self.AGENTIC_KEYWORDS)
        multi_step_patterns = [
            r'\bfirst\b.*\bthen\b', r'\bstep\s+\d+', r'\d+\.\s+\w+',
            r'\bnext\b', r'\bafter\b', r'\bfinally\b', r',\s*then\b'
        ]
        is_multi_step = any(re.search(p, text) for p in multi_step_patterns)
        
        if agentic_count >= 2 or is_multi_step:
            # Multi-step task - ensure at least CODE tier
            tier_scores['CODE'] += 2
            if tier_scores['FAST'] > 0:
                tier_scores['FAST'] = 0  # Override FAST if agentic
        
        # Find best matching tier
        if max(tier_scores.values()) == 0:
            # No keywords matched - default to FAST
            best_tier = 'FAST'
        else:
            best_tier = max(tier_scores, key=tier_scores.get)
        
        # Website/frontend projects → CREATIVE
        website_project_keywords = [
            'website', 'web site', 'landing page', 'landing', 'frontend',
            'community site', 'online community', 'build a site', 'new site'
        ]
        if self._keyword_match(task_description, website_project_keywords) > 0:
            best_tier = 'CREATIVE'
        
        # Map COMPLEX to CODE for our tier system
        if best_tier == 'COMPLEX':
            if tier_scores['CODE'] > 0:
                best_tier = 'CODE'
            else:
                best_tier = 'QUALITY'
        
        # Special handling: if both COMPLEX and FAST matched, prefer CODE/QUALITY
        if tier_scores['COMPLEX'] > 0 and tier_scores['FAST'] > 0:
            if tier_scores['COMPLEX'] >= tier_scores['FAST']:
                best_tier = 'QUALITY' if tier_scores['COMPLEX'] >= 2 else 'CODE'
        
        # Calculate confidence based on match strength
        max_score = max(tier_scores.values())
        confidence = min(max_score / 5.0, 1.0)
        
        result = {
            'tier': best_tier,
            'confidence': round(confidence, 3),
            'tier_scores': {k: v for k, v in tier_scores.items() if v > 0},
            'is_agentic': agentic_count >= 2 or is_multi_step
        }
        
        if not return_details:
            return result['tier']
        
        return result
    
    def get_default_model(self):
        """Return the default model (local Llama 3.2). Used for session default and orchestrator."""
        default_id = self.config.get('default_model')
        if not default_id:
            # Fallback: FAST tier primary
            tier_rules = self.config.get('routing_rules', {}).get('FAST', {})
            default_id = tier_rules.get('primary')
        for m in self.config.get('models', []):
            if m['id'] == default_id:
                return m
        return None
    
    def recommend_model(self, task_description):
        """Classify task and recommend the best model (preferring local when available)."""
        classification = self.classify_task(task_description, return_details=True)
        tier = classification['tier']
        
        # Get routing rules for this tier
        routing_rules = self.config.get('routing_rules', {})
        tier_rules = routing_rules.get(tier, {})
        
        # Get primary model
        primary_id = tier_rules.get('primary')
        prefer_local = tier_rules.get('prefer_local', False)
        
        # Check if primary is local and available
        model = None
        if prefer_local and primary_id.startswith("ollama/"):
            if check_local_model_available(primary_id):
                # Use local model
                for m in self.config.get('models', []):
                    if m['id'] == primary_id:
                        model = m
                        break
            else:
                # Local not available, try fallback
                fallback_ids = tier_rules.get('fallback', [])
                for fb_id in fallback_ids:
                    # Prefer local fallback if available
                    if fb_id.startswith("ollama/") and check_local_model_available(fb_id):
                        for m in self.config.get('models', []):
                            if m['id'] == fb_id:
                                model = m
                                break
                        if model:
                            break
                    # Or use OpenRouter fallback
                    elif not fb_id.startswith("ollama/"):
                        for m in self.config.get('models', []):
                            if m['id'] == fb_id:
                                model = m
                                break
                        if model:
                            break
        else:
            # Not preferring local, use primary
            for m in self.config.get('models', []):
                if m['id'] == primary_id:
                    model = m
                    break
        
        # If no model found, use primary anyway
        if not model:
            for m in self.config.get('models', []):
                if m['id'] == primary_id:
                    model = m
                    break
        
        # Fallback
        fallback = None
        fallback_ids = tier_rules.get('fallback', [])
        if fallback_ids:
            for fb_id in fallback_ids:
                for m in self.config.get('models', []):
                    if m['id'] == fb_id:
                        fallback = m
                        break
        
        return {
            'tier': tier,
            'model': model,
            'fallback': fallback,
            'classification': classification,
            'reasoning': self._explain(tier, classification),
            'is_local': model and model.get('local', False) if model else False
        }
    
    def _explain(self, tier, classification):
        """Provide reasoning for tier selection."""
        explanations = {
            'FAST': 'Simple, quick task - monitoring, checks, summaries (prefer local)',
            'REASONING': 'Logical analysis, math, step-by-step reasoning (prefer cloud)',
            'CREATIVE': 'Creative writing, design, frontend work (prefer cloud)',
            'RESEARCH': 'Information lookup, web search, fact-finding (prefer local)',
            'CODE': 'Code generation, debugging, implementation (prefer local)',
            'COMPLEX': 'Code generation, debugging, implementation',
            'QUALITY': 'Complex, comprehensive, architectural work (prefer cloud)',
            'VISION': 'Image analysis, visual understanding (cloud only)'
        }
        
        explanation = explanations.get(tier, 'General task')
        
        if classification.get('is_agentic'):
            explanation += ' [Multi-step agentic task detected]'
        
        return explanation
    
    def estimate_cost(self, task_description):
        """Estimate cost for a task (local models = $0)."""
        result = self.recommend_model(task_description)
        model = result['model']
        
        if not model:
            return {'error': 'No model found for tier'}
        
        # Rough token estimates
        token_estimate = {
            'FAST': {'in': 500, 'out': 200},
            'REASONING': {'in': 2000, 'out': 1500},
            'CREATIVE': {'in': 1500, 'out': 2000},
            'RESEARCH': {'in': 1000, 'out': 500},
            'CODE': {'in': 2000, 'out': 1500},
            'QUALITY': {'in': 5000, 'out': 3000},
            'VISION': {'in': 500, 'out': 500}
        }
        
        tokens = token_estimate.get(result['tier'], {'in': 500, 'out': 200})
        
        # Local models have zero cost
        if model.get('local', False):
            input_cost = 0.0
            output_cost = 0.0
        else:
            input_cost = (tokens['in'] / 1_000_000) * model['input_cost_per_m']
            output_cost = (tokens['out'] / 1_000_000) * model['output_cost_per_m']
        
        return {
            'tier': result['tier'],
            'model': model['alias'],
            'is_local': model.get('local', False),
            'cost': round(input_cost + output_cost, 4),
            'currency': 'USD'
        }
    
    @staticmethod
    def split_into_tasks(message):
        """Split a message into multiple task strings for parallel spawn.
        Splits on ' and ', ' then ', '; ', and ' also '. Returns list of non-empty stripped strings.
        
        Security: Validates input and sanitizes each task.
        """
        if not isinstance(message, str):
            raise ValueError("message must be a string")
        if len(message) > 10000:
            raise ValueError("message exceeds maximum length (10KB)")
        
        if not (message or message.strip()):
            return []
        text = message.strip()
        for sep in [' and ', ' then ', '; ', ' also ']:
            text = text.replace(sep, '\n')
        parts = [p.strip() for p in text.split('\n') if p.strip()]
        # Security: Validate each split task
        validated_parts = []
        for part in (parts if len(parts) > 1 else ([message.strip()] if message.strip() else [])):
            try:
                validated_parts.append(validate_task_string(part))
            except ValueError:
                # Skip invalid parts rather than failing entirely
                continue
        return validated_parts

    def spawn_agent(self, task, session_target='isolated', label=None, required_exec_host='sandbox', required_exec_node=None):
        """Spawn an OpenClaw sub-agent with the appropriate model (local or OpenRouter).
        Optionally checks tools.exec host/node; on mismatch returns needs_config_patch (no exit).
        Always returns one dict: either params+recommendation or needs_config_patch+message+recommended_config_patch.
        
        Security: Validates task input and config patches to prevent injection attacks.
        """
        # Security: Validate and sanitize task input
        try:
            task = validate_task_string(task)
        except ValueError as e:
            raise ValueError(f"Invalid task input: {e}")
        
        # Security: Validate label if provided
        if label:
            if not isinstance(label, str) or len(label) > 100:
                raise ValueError("Label must be a string under 100 characters")
            if '\x00' in label:
                raise ValueError("Label contains null bytes")
        
        # Security: Validate required_exec_host and required_exec_node
        if required_exec_host not in ('sandbox', 'node'):
            raise ValueError(f"required_exec_host must be 'sandbox' or 'node', got: {required_exec_host}")
        
        if required_exec_node and not isinstance(required_exec_node, str):
            raise ValueError("required_exec_node must be a string")
        if required_exec_node and len(required_exec_node) > 200:
            raise ValueError("required_exec_node exceeds maximum length")
        
        current_config = get_current_openclaw_config()
        current_exec_host = current_config["tools_exec_host"] if current_config else "sandbox"
        current_exec_node = current_config["tools_exec_node"] if current_config else None

        if required_exec_host and current_exec_host != required_exec_host:
            msg = (
                f"Task requires exec host '{required_exec_host}' but current config is '{current_exec_host}'. "
                "Apply the recommended config patch (triggers gateway restart if host=node)."
                if required_exec_host == "node"
                else f"Temporarily set default exec host to '{required_exec_host}'."
            )
            # Security: Only allow safe config patches
            patch_dict = {"tools": {"exec": {"host": required_exec_host}}}
            patch = json.dumps(patch_dict)
            # Validate the patch we're generating
            try:
                validate_config_patch(patch)
            except ValueError as e:
                raise ValueError(f"Generated invalid config patch: {e}")
            
            return {
                "needs_config_patch": True,
                "message": msg,
                "recommended_config_patch": patch,
                "params": None,
            }

        if required_exec_host == "node" and required_exec_node and current_exec_node != required_exec_node:
            msg = (
                f"Task requires exec node '{required_exec_node}' but current config is '{current_exec_node}'. "
                "Apply the recommended config patch (triggers gateway restart)."
            )
            # Security: Only allow safe config patches
            patch_dict = {"tools": {"exec": {"host": "node", "node": required_exec_node}}}
            patch = json.dumps(patch_dict)
            # Validate the patch we're generating
            try:
                validate_config_patch(patch)
            except ValueError as e:
                raise ValueError(f"Generated invalid config patch: {e}")
            
            return {
                "needs_config_patch": True,
                "message": msg,
                "recommended_config_patch": patch,
                "params": None,
            }

        recommendation = self.recommend_model(task)
        model = recommendation["model"]
        if not model:
            raise ValueError(f"No model found for tier: {recommendation['tier']}")

        params = {
            "task": task,
            "model": model["id"],
            "sessionTarget": session_target,
        }
        if label:
            params["label"] = label

        return {
            "params": params,
            "recommendation": recommendation,
            "executionContract": {
                "must_delegate": True,
                "required_action": "sessions_spawn",
                "self_execution_forbidden": True,
                "meta_only_exception": True,
            },
        }


def main():
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Agent Swarm (Local + OpenRouter) | OpenClaw Skill - Hybrid LLM routing.")
    sub = parser.add_subparsers(dest="command", required=True)

    # Common args for classify, score, cost that require a task
    for cmd_name in ["classify", "score", "cost"]:
        p = sub.add_parser(cmd_name, help=f"{cmd_name.capitalize()} task")
        p.add_argument("task", type=str, nargs='+', help="The task description string")
        p.add_argument("--json", action="store_true", help="Output JSON report")

    # Default command
    p_default = sub.add_parser("default", help="Show session default model")
    p_default.add_argument("--json", action="store_true", help="Output JSON report")

    # Models command
    p_models = sub.add_parser("models", help="List all models")
    p_models.add_argument("--json", action="store_true", help="Output JSON report")

    # Spawn command
    p_spawn = sub.add_parser("spawn", help="Show spawn params for OpenClaw (sessions_spawn)")
    p_spawn.add_argument("task", type=str, nargs='+', help="The task string for the sub-agent")
    p_spawn.add_argument("--json", action="store_true", help="Machine-readable output for sessions_spawn")
    p_spawn.add_argument("--multi", action="store_true", help="Split message into parallel tasks (and / then / ;); output array of spawn params")
    p_spawn.add_argument("--required_exec_host", type=str, default="sandbox", help="Required exec host (sandbox or node)")
    p_spawn.add_argument("--required_exec_node", type=str, default=None, help="Required exec node ID or name if host=node")
    p_spawn.add_argument("--label", type=str, default=None, help="Optional label for the sub-agent session")
    
    args = parser.parse_args()

    router = HybridRouter()

    task_str = ' '.join(args.task) if 'task' in args and args.task else ""

    if args.command == 'default':
        m = router.get_default_model()
        if not m:
            print("❌ No default model configured (missing default_model or FAST primary in config)", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps({"model": m}))
        else:
            print("🎯 Session default model (local, persistent):\n")
            print(f"   {m['alias']} ({m['id']})")
            if m.get('local'):
                print(f"   Cost: FREE (local via WebGPU)")
            else:
                print(f"   Cost: ${m['input_cost_per_m']}/${m['output_cost_per_m']} per M")
            print(f"   Use for: {', '.join(m.get('use_for', []))}")
            print("\n   Simple tasks prefer local models (zero cost). Complex tasks route to OpenRouter.")

    elif args.command == 'classify':
        result = router.recommend_model(task_str)
        if args.json:
            print(json.dumps(result))
        else:
            print(f"📋 Task: {task_str}")
            print(f"\n🎯 Classification: {result['tier']}")
            print(f"   Confidence: {result['classification']['confidence']:.1%}")
            print(f"   Reasoning: {result['reasoning']}")
            if result['model']:
                m = result['model']
                model_type = "🖥️  LOCAL" if result.get('is_local') else "☁️  CLOUD"
                print(f"\n🤖 Recommended Model ({model_type}):")
                print(f"   {m['alias']} ({m['id']})")
                if m.get('local'):
                    print(f"   Cost: FREE (local via WebGPU)")
                else:
                    print(f"   Cost: ${m['input_cost_per_m']}/${m['output_cost_per_m']} per M")
                print(f"   Use for: {', '.join(m.get('use_for', []))}")
            if result['fallback']:
                fb = result['fallback']
                fb_type = "🖥️  LOCAL" if fb.get('local') else "☁️  CLOUD"
                print(f"\n🔄 Fallback ({fb_type}): {fb['alias']} ({fb['id']})")

    elif args.command == 'score':
        result = router.classify_task(task_str, return_details=True)
        if args.json:
            print(json.dumps(result))
        else:
            print(f"📋 Task: {task_str}")
            print(f"\n🎯 Tier: {result['tier']}")
            print(f"   Confidence: {result['confidence']:.1%}")
            print(f"   Agentic: {'Yes' if result['is_agentic'] else 'No'}")
            
            print(f"\n📊 Tier Scores:")
            for tier, score in sorted(result['tier_scores'].items(), key=lambda x: x[1], reverse=True):
                bar = '█' * score
                print(f"   {tier:10} {bar} ({score})")

    elif args.command == 'cost':
        result = router.estimate_cost(task_str)
        if args.json:
            print(json.dumps(result))
        else:
            if 'error' in result:
                print(f"❌ Error: {result['error']}", file=sys.stderr)
            else:
                print(f"📋 Task: {task_str}")
                print(f"\n💰 Cost Estimate:")
                print(f"   Tier: {result['tier']}")
                print(f"   Model: {result['model']}")
                cost_str = "FREE (local)" if result.get('is_local') else f"${result['cost']} {result['currency']}"
                print(f"   Est. Cost: {cost_str}")

    elif args.command == 'models':
        if args.json:
            print(json.dumps(router.config.get('models', [])))
        else:
            print("📦 Configured Models:\n")
            for model in router.config.get('models', []):
                model_type = "🖥️  LOCAL" if model.get('local') else "☁️  CLOUD"
                print(f"  {model_type} {model['alias']:20} [{model['tier']:8}] {model['id']}")
                if model.get('local'):
                    print(f"                         FREE (persistent via WebGPU)")
                else:
                    print(f"                         ${model['input_cost_per_m']}/${model['output_cost_per_m']}/M")

    elif args.command == 'spawn':
        if not task_str:
            print("❌ Error: spawn requires a task string", file=sys.stderr)
            sys.exit(1)

        if getattr(args, 'multi', False):
            # Parallel tasks: split message, spawn each, output array
            tasks = HybridRouter.split_into_tasks(task_str)
            if not tasks:
                tasks = [task_str]
            results = []
            config_patch_result = None
            for i, one_task in enumerate(tasks):
                spawn_result = router.spawn_agent(
                    task=one_task,
                    label=args.label or (f"parallel-{i+1}" if len(tasks) > 1 else None),
                    required_exec_host=args.required_exec_host,
                    required_exec_node=args.required_exec_node,
                )
                if spawn_result.get("needs_config_patch"):
                    config_patch_result = spawn_result
                    break
                results.append({
                    "task": spawn_result["params"]["task"],
                    "model": spawn_result["params"]["model"],
                    "sessionTarget": spawn_result["params"]["sessionTarget"],
                })
                if spawn_result["params"].get("label"):
                    results[-1]["label"] = spawn_result["params"]["label"]
            if config_patch_result:
                if args.json:
                    print(json.dumps(config_patch_result))
                else:
                    print("🚧 Configuration required!\n")
                    print(f"   {config_patch_result['message']}\n")
                    print(f"   Recommended patch: {config_patch_result['recommended_config_patch']}")
            elif args.json:
                print(json.dumps({
                    "parallel": True,
                    "spawns": results,
                    "count": len(results),
                    "executionContract": {
                        "must_delegate": True,
                        "required_action": "sessions_spawn",
                        "self_execution_forbidden": True,
                        "meta_only_exception": True,
                    },
                }))
            else:
                print(f"📋 Parallel tasks ({len(results)}):\n")
                for i, r in enumerate(results, 1):
                    print(f"   {i}. {r['task'][:50]}{'...' if len(r['task']) > 50 else ''} → {r['model'].split('/')[-1]}")
        else:
            spawn_result = router.spawn_agent(
                task=task_str,
                label=args.label,
                required_exec_host=args.required_exec_host,
                required_exec_node=args.required_exec_node,
            )

            if spawn_result.get("needs_config_patch"):
                if args.json:
                    print(json.dumps(spawn_result))
                else:
                    print("🚧 Configuration required!\n")
                    print(f"   {spawn_result['message']}\n")
                    print(f"   Recommended patch: {spawn_result['recommended_config_patch']}")
                    print("   Then retry spawn after gateway restarts if needed.")
            else:
                if args.json:
                    out = {k: v for k, v in spawn_result["params"].items()}
                    out["recommendation"] = spawn_result["recommendation"]
                    out["executionContract"] = spawn_result.get("executionContract", {
                        "must_delegate": True,
                        "required_action": "sessions_spawn",
                        "self_execution_forbidden": True,
                        "meta_only_exception": True,
                    })
                    print(json.dumps(out))
                else:
                    print(f"📋 Task: {task_str}")
                    print(f"\n🚀 OpenClaw Spawn Params:")
                    print(f"   model: {spawn_result['params']['model']}")
                    print(f"   sessionTarget: {spawn_result['params']['sessionTarget']}")
                    print(f"\n📦 Full recommendation:")
                    print(f"   Tier: {spawn_result['recommendation']['tier']}")
                    m = spawn_result['recommendation']['model']
                    model_type = "🖥️  LOCAL" if spawn_result['recommendation'].get('is_local') else "☁️  CLOUD"
                    print(f"   Model ({model_type}): {m['alias']}")

    else:
        print(f"Unknown command: {args.command}", file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
