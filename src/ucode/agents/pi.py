"""Pi coding agent: writes the user's ~/.pi/agent/models.json with Databricks-backed providers.

Pi (https://pi.dev) is a multi-provider coding agent. We register four
providers in its `models.json`, each speaking the API dialect best suited to
that family's gateway path:

- `databricks-claude`  (api: anthropic-messages)       → /ai-gateway/anthropic
- `databricks-openai`  (api: openai-responses)         → /ai-gateway/openai/v1
- `databricks-gemini`  (api: google-generative-ai)     → /ai-gateway/gemini/v1beta
- `databricks-mlflow`  (api: openai-completions)       → /ai-gateway/mlflow/v1

Per-provider `compat` flags work around fields the gateway translators reject:

- claude: `supportsEagerToolInputStreaming: false` — the Anthropic translator
  rejects `tools[].eager_input_streaming` on the streaming + tools path that
  pi uses for every request. With this flag pi omits the per-tool field and
  sends the legacy `anthropic-beta: fine-grained-tool-streaming-...` header
  instead, which the gateway accepts.
- mlflow: `supportsStore: false`, `supportsStrictMode: false`,
  `supportsLongCacheRetention: false` and `maxTokensField: "max_tokens"` —
  the MLflow chat-completions gateway rejects OpenAI's `store` field,
  `tools[].function.strict`, the `prompt_cache_retention` /
  `prompt_cache_key` fields Pi sends for long cache retention, and (for Qwen,
  gpt-oss, Gemma and Llama) `max_completion_tokens` (`json: unknown field`,
  HTTP 400). Every MLflow model accepts `max_tokens`. Also
  `supportsDeveloperRole: false`: OSS chat templates (Qwen 3.5) reject the
  `developer` role, so Pi sends instructions as `system`.
- openai: no `compat` flags needed, but the per-model `thinkingLevelMap`
  matters — see `_pi_gpt_model_entry`. Declaring `reasoning: true` without an
  off-state makes Pi send `reasoning: {effort: "none"}`, which `gpt-5`,
  `gpt-5-mini`, `gpt-5-nano` and `gpt-5-5-pro` reject with a 400.

The `databricks-mlflow` provider carries validated MLflow chat-completions
models discovered upstream. Per-model reasoning and token metadata comes from
the persisted gateway capability specs, with conservative static fallback.
At launch this provider alone is routed through a loopback repair proxy because
some models (notably Inkling) omit the terminal `finish_reason` Pi requires.

Each provider's `apiKey` is pi's `!command` config value rather than a baked
bearer, so pi mints one per request via `ug auth-token` and nothing that
expires is written to `models.json` (the on-demand model OpenCode's auth plugin
already uses). A token still reaches the process environment: `launch` exports
`OAUTH_TOKEN` as before.
"""

from __future__ import annotations

import ipaddress
import os
import shlex
import signal
import threading
from pathlib import Path
from typing import cast
from urllib.parse import urlparse

from ucode.agents import _mlflow_proxy
from ucode.config_io import (
    APP_DIR,
    ToolSpec,
    backup_existing_file,
    deep_merge_dict,
    read_json_safe,
    write_json_file,
)
from ucode.constants import AI_GATEWAY_REQUEST_TAGS_HEADER
from ucode.databricks import (
    ANTHROPIC_FAMILIES,
    build_auth_token_argv,
    build_pi_base_urls,
    classify_model_family,
    claude_model_capabilities,
    claude_model_version,
    discover_claude_models_unbucketed,
    get_databricks_token,
    gpt_model_token_limits,
    model_is_reasoning,
    model_token_limits,
    preferred_gpt_model,
)
from ucode.os_compatibility import subprocess_cross_os
from ucode.state import mark_tool_managed, save_state
from ucode.telemetry import agent_version, ucode_user_agent, ug_version
from ucode.ui import print_warning
from ucode.workspace import workspace_headers

from .args import LaunchOptions
from .request_tags import request_tags_header_value

# Use Pi's standard user configuration directory. Keep ucode's backups and
# state under APP_DIR, but let Pi share the user's normal ~/.pi/agent config.
PI_CONFIG_DIR = Path.home() / ".pi" / "agent"
PI_CONFIG_PATH = PI_CONFIG_DIR / "models.json"
PI_SETTINGS_PATH = PI_CONFIG_DIR / "settings.json"
PI_BACKUP_PATH = APP_DIR / "pi-models.backup.json"
PI_SETTINGS_BACKUP_PATH = APP_DIR / "pi-settings.backup.json"
_CONFIG_WRITE_LOCK = threading.RLock()

SPEC: ToolSpec = {
    "binary": "pi",
    "package": "@earendil-works/pi-coding-agent",
    "display": "Pi",
    "config_path": PI_CONFIG_PATH,
    "backup_path": PI_BACKUP_PATH,
}

PROVIDER_NAMES = (
    "databricks-claude",
    "databricks-openai",
    "databricks-gemini",
    "databricks-mlflow",
)

PROVIDER_KEYS: list[list[str]] = [["providers", name] for name in PROVIDER_NAMES]

# Old provider names earlier ucode versions wrote; cleaned up on each write so
# users don't end up with stale entries pointing at routes that 400.
LEGACY_PROVIDER_NAMES = ("databricks-anthropic", "databricks-codex", "databricks-oss")


def _resolve_model_selector(
    model: str,
    claude_models: dict[str, str],
    codex_models: list[str],
    gemini_models: list[str],
    oss_models: list[str],
    claude_model_ids: list[str] | None = None,
) -> str:
    """Return a Pi model selector in `<provider>/<model>` form when possible."""
    for name in PROVIDER_NAMES:
        if model.startswith(f"{name}/"):
            return model
    all_claude_models = set(claude_models.values())
    all_claude_models.update(claude_model_ids or [])
    if model in all_claude_models:
        return f"databricks-claude/{model}"
    if model in codex_models:
        return f"databricks-openai/{model}"
    if model in gemini_models:
        return f"databricks-gemini/{model}"
    if model in oss_models:
        return f"databricks-mlflow/{model}"
    return model


# Claude models older than 4.5 (e.g. Opus 4.1) reject Pi's Anthropic request
# shape: `cache_control.ephemeral.ttl` and `thinking.enabled.display` are
# "Extra inputs are not permitted" (HTTP 400), and tool calls are refused even
# with those disabled. Pi only; other agents still list them.
_PI_MIN_CLAUDE_VERSION = (4, 5)

# Databricks AI Gateway enforces a 32 MiB HTTP body limit across model routes.
# Pi's default per-image resize cap is 4.5 MiB base64, which keeps lossless PNG
# screenshots at 1.5-4 MB each so ~15-20 screenshots breach 32 MiB well before
# token-based compaction triggers (Pi estimates every image at 1,200 tokens).
# Capping encoded images at 750 KiB forces JPEG compression on dense screenshots.
_PI_GATEWAY_MAX_REQUEST_BYTES = 33_554_432
_PI_IMAGE_INPUT_LIMITS: dict[str, object] = {
    "maxRequestBytes": _PI_GATEWAY_MAX_REQUEST_BYTES,
    "images": {
        "resize": {
            "maxWidth": 2000,
            "maxHeight": 2000,
            "maxBytes": 786_432,
            "jpegQuality": 80,
        }
    },
}


def _pi_supports_claude_model(model_id: str) -> bool:
    version = claude_model_version(model_id)
    return version is None or version >= _PI_MIN_CLAUDE_VERSION


def _pi_claude_model_entry(model_id: str) -> dict:
    """Build a Claude entry with explicit limits.

    Databricks model ids do not match Pi's built-in Anthropic ids, so a bare
    custom entry silently gets Pi's 128k context / 4k output defaults.
    """
    capabilities = claude_model_capabilities(model_id)
    entry: dict = {
        "id": model_id,
        "reasoning": True,
        "input": ["text", "image"],
        "inputLimits": _PI_IMAGE_INPUT_LIMITS,
        "contextWindow": capabilities.context,
        "maxTokens": capabilities.output,
    }
    if capabilities.force_adaptive_thinking:
        entry["compat"] = {"forceAdaptiveThinking": True}
        # Pi hides its extended levels unless custom models declare them.
        # `max`, `xhigh`, disabling thinking, and `minimal` vary by model (see
        # `ClaudeModelCapabilities` for the sources).
        level_map: dict[str, str | None] = {}
        if capabilities.supports_max_thinking:
            level_map["max"] = "max"
        if capabilities.supports_xhigh_thinking:
            level_map["xhigh"] = "xhigh"
        if not capabilities.supports_disabled_thinking:
            level_map["off"] = None
        if not capabilities.supports_minimal_thinking:
            level_map["minimal"] = None
        entry["thinkingLevelMap"] = level_map
    return entry


_OSS_SAFE_LIMITS = {"context": 128_000, "output": 8_192}

# Pi thinking levels for MLflow chat-completions models, from the Databricks
# reasoning-models doc. Pi sends `reasoning_effort` with the mapped value, and
# for `off` only when it maps to a string; with no `off` mapping, hybrid models
# below would silently reason at their `max` default instead of stopping.
# Values the doc says "fall back to max" or alias another tier are hidden.
_HYBRID_LOW_HIGH_MAX: dict[str, str | None] = {
    "off": "none",
    "minimal": None,
    "low": "low",
    "medium": None,
    "high": "high",
    "max": "max",
}
_REASONING_ONLY_LOW_HIGH_MAX: dict[str, str | None] = {**_HYBRID_LOW_HIGH_MAX, "off": None}
_OSS_THINKING_LEVEL_MAPS: dict[str, dict[str, str | None]] = {
    # Hybrid: `none` disables reasoning; omitted effort defaults to `max`.
    "glm-5-2": {**_HYBRID_LOW_HIGH_MAX, "low": None},
    "kimi-k3": _HYBRID_LOW_HIGH_MAX,
    "deepseek-v4-pro-0813": _HYBRID_LOW_HIGH_MAX,
    "deepseek-v4-flash-0731": _HYBRID_LOW_HIGH_MAX,
    "deepseek-v4-1-flash": {**_HYBRID_LOW_HIGH_MAX, "xhigh": "xhigh"},
    "deepseek-v4-1-flash-1017": {**_HYBRID_LOW_HIGH_MAX, "xhigh": "xhigh"},
    # Reasoning-only: GLM 5.3 rejects `none` with HTTP 400.
    "glm-5-3": _REASONING_ONLY_LOW_HIGH_MAX,
    "glm-5-3-flash": _REASONING_ONLY_LOW_HIGH_MAX,
    # GPT OSS accepts low/medium/high and ignores `none` (it always reasons).
    "gpt-oss-120b": {"off": None, "minimal": None},
    "gpt-oss-20b": {"off": None, "minimal": None},
    # Inkling: minimal=low and xhigh=max share tiers; `none` does not disable.
    "inkling": {"off": None, "minimal": None, "xhigh": None, "max": "max"},
}


def _oss_thinking_level_map(model_id: str) -> dict[str, str | None] | None:
    normalized_id = model_id.rsplit("/", 1)[-1].lower()
    for prefix in ("system.ai.", "databricks-"):
        if normalized_id.startswith(prefix):
            normalized_id = normalized_id[len(prefix) :]
            break
    level_map = _OSS_THINKING_LEVEL_MAPS.get(normalized_id.replace(".", "-"))
    return dict(level_map) if level_map is not None else None


def _positive_int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


def _oss_specs_by_id(raw_specs: object) -> dict[str, dict[str, object]]:
    if not isinstance(raw_specs, list):
        return {}
    specs: dict[str, dict[str, object]] = {}
    for raw_spec in raw_specs:
        if not isinstance(raw_spec, dict):
            continue
        typed_spec = cast(dict[str, object], raw_spec)
        model_id = typed_spec.get("id")
        reasoning = typed_spec.get("reasoning")
        context = typed_spec.get("context_window")
        output = typed_spec.get("max_tokens")
        valid_limits = all(
            value is None or _positive_int(value) is not None for value in (context, output)
        )
        if (
            isinstance(model_id, str)
            and model_id
            and isinstance(reasoning, bool)
            and "context_window" in typed_spec
            and "max_tokens" in typed_spec
            and valid_limits
            and model_id not in specs
        ):
            specs[model_id] = typed_spec
    return specs


def _pi_oss_model_entry(model_id: str, spec: dict[str, object] | None = None) -> dict:
    """Build a Pi MLflow model entry from discovered or static capabilities.

    A valid discovered boolean overrides static reasoning. Any discovered spec
    receives a complete conservative limit pair, so missing capability fields
    cannot leave a validated model effectively uncapped. Missing specs retain
    the existing static GLM/Kimi/DeepSeek behavior, while unknown models remain bare.
    """
    entry: dict = {"id": model_id}
    static_limits = model_token_limits(model_id)
    static_reasoning = model_is_reasoning(model_id)

    reasoning = spec.get("reasoning") if isinstance(spec, dict) else None
    if not isinstance(reasoning, bool):
        reasoning = static_reasoning
    if reasoning:
        entry["reasoning"] = True
        level_map = _oss_thinking_level_map(model_id)
        if level_map is not None:
            entry["thinkingLevelMap"] = level_map

    context = _positive_int(spec.get("context_window")) if isinstance(spec, dict) else None
    output = _positive_int(spec.get("max_tokens")) if isinstance(spec, dict) else None
    if isinstance(spec, dict):
        entry["contextWindow"] = context or (
            static_limits.get("context") if static_limits else _OSS_SAFE_LIMITS["context"]
        )
        entry["maxTokens"] = output or (
            static_limits.get("output") if static_limits else _OSS_SAFE_LIMITS["output"]
        )
    elif static_limits:
        entry["contextWindow"] = static_limits["context"]
        entry["maxTokens"] = static_limits["output"]
    return entry


def _responses_specs_by_id(raw_specs: object) -> dict[str, dict[str, object]]:
    if not isinstance(raw_specs, list):
        return {}
    specs: dict[str, dict[str, object]] = {}
    for raw_spec in raw_specs:
        if not isinstance(raw_spec, dict):
            continue
        typed_spec = cast(dict[str, object], raw_spec)
        model_id = typed_spec.get("id")
        context = _positive_int(typed_spec.get("context_window"))
        if isinstance(model_id, str) and model_id and context is not None:
            specs.setdefault(model_id, typed_spec)
    return specs


def _pi_gpt_model_entry(model_id: str, spec: dict[str, object] | None = None) -> dict:
    """Build a Pi Responses model entry with explicit limits and reasoning.

    Gateway ids aren't in Pi's built-in catalog, so capabilities must be
    declared here rather than inherited from Pi's vendor model registry.

    `thinkingLevelMap: {"off": None}` is required alongside `reasoning: True`.
    When a model declares `reasoning` but no off-state, Pi's Responses builder
    falls back to `reasoning: {effort: "none"}` for the thinking-off case
    (`pi-ai/dist/api/openai-responses.js`, the `thinkingLevelMap?.off !== null`
    branch). `"none"` is only valid on gpt-5.1+, so `gpt-5`, `gpt-5-mini`,
    `gpt-5-nano` and `gpt-5-5-pro` reject every request with
    `BAD_REQUEST: Unsupported value: 'none' is not supported with the 'gpt-5'
    model`. An explicit `None` makes Pi omit `reasoning` entirely, which the
    gateway accepts for all ids.
    """
    limits = gpt_model_token_limits(model_id)
    discovered_context = (
        _positive_int(spec.get("context_window")) if isinstance(spec, dict) else None
    )
    entry: dict = {
        "id": model_id,
        "contextWindow": discovered_context or limits["context"],
        "maxTokens": limits["output"],
    }
    normalized_id = model_id.rsplit("/", 1)[-1].lower()
    for prefix in ("system.ai.", "databricks-"):
        if normalized_id.startswith(prefix):
            normalized_id = normalized_id[len(prefix) :]
            break
    normalized_id = normalized_id.replace(".", "-")
    # Thinking levels follow the Databricks reasoning-models doc, then Pi's
    # catalog (https://pi.dev/api/models) for models the doc does not list.
    # Levels left unmapped use the provider default through `high`; `xhigh`
    # and `max` must be declared.
    if normalized_id in {"grok-4-5", "grok-4-6", "grok-4-7"}:
        # Grok 4.5-4.7 are reasoning-only and support low/medium/high/xhigh;
        # hide Pi's unsupported off/minimal/max choices instead of mapping them
        # to values the gateway would reject.
        entry["reasoning"] = True
        entry["thinkingLevelMap"] = {
            "off": None,
            "minimal": None,
            "xhigh": "xhigh",
            "max": None,
        }
    elif normalized_id == "gpt-6-astra":
        # Astra is reasoning-only. Its supported effort values are
        # low/medium/high/xhigh/max; suppress Pi's off/minimal choices rather
        # than sending values the gateway does not support.
        entry["reasoning"] = True
        entry["input"] = ["text", "image"]
        entry["thinkingLevelMap"] = {
            "off": None,
            "minimal": None,
            "xhigh": "xhigh",
            "max": "max",
        }
    elif normalized_id.startswith(
        (
            "gpt-6-sol",
            "gpt-6-terra",
            "gpt-6-luna",
            "gpt-6-1-sol",
            "gpt-6-1-terra",
            "gpt-6-1-luna",
        )
    ):
        # GPT-6 and GPT-6.1 Sol/Terra/Luna (6.1 IDs normalise to 6-1) accept
        # none/low/medium/high/xhigh/max on Databricks; `minimal` is rejected.
        # GPT-6.1 Luna is undocumented and follows GPT-6.1 Terra.
        entry["reasoning"] = True
        entry["input"] = ["text", "image"]
        entry["thinkingLevelMap"] = {
            "off": "none",
            "minimal": None,
            "low": "low",
            "medium": "medium",
            "high": "high",
            "xhigh": "xhigh",
            "max": "max",
        }
    elif normalized_id.startswith("gpt-5-6-") or (
        normalized_id.startswith("gpt-5-5") and not normalized_id.startswith("gpt-5-5-pro")
    ):
        # GPT-5.5 and GPT-5.6 support none/low/medium/high/xhigh; GPT-5.6
        # additionally supports max. Pi's minimal level is not an API value.
        entry["reasoning"] = True
        entry["input"] = ["text", "image"]
        entry["thinkingLevelMap"] = {
            "off": "none",
            "minimal": None,
            "xhigh": "xhigh",
        }
        if normalized_id.startswith("gpt-5-6-"):
            entry["thinkingLevelMap"]["max"] = "max"
    elif normalized_id.startswith(("gpt-5-2-pro", "gpt-5-4-pro", "gpt-5-5-pro")):
        # Pro variants support medium/high/xhigh only.
        entry["reasoning"] = True
        entry["input"] = ["text", "image"]
        entry["thinkingLevelMap"] = {
            "off": None,
            "minimal": None,
            "low": None,
            "xhigh": "xhigh",
        }
    elif normalized_id.startswith("gpt-5-pro"):
        # GPT-5 Pro supports only `high`.
        entry["reasoning"] = True
        entry["input"] = ["text", "image"]
        entry["thinkingLevelMap"] = {
            "off": None,
            "minimal": None,
            "low": None,
            "medium": None,
        }
    elif normalized_id.startswith("gpt-5-3-codex-spark"):
        # Codex Spark supports low/medium/high/xhigh, with no `none`.
        entry["reasoning"] = True
        entry["input"] = ["text", "image"]
        entry["thinkingLevelMap"] = {
            "off": None,
            "minimal": None,
            "xhigh": "xhigh",
        }
    elif normalized_id.startswith(("gpt-5-2", "gpt-5-3", "gpt-5-4")):
        # GPT-5.2, GPT-5.3 Codex, and GPT-5.4 standard/mini/nano support
        # none/low/medium/high/xhigh.
        entry["reasoning"] = True
        entry["input"] = ["text", "image"]
        entry["thinkingLevelMap"] = {
            "off": "none",
            "minimal": None,
            "xhigh": "xhigh",
        }
    elif normalized_id.startswith("gpt-5-1"):
        # GPT-5.1 introduced `none` but has no `minimal` or `xhigh`.
        entry["reasoning"] = True
        entry["input"] = ["text", "image"]
        entry["thinkingLevelMap"] = {"off": "none", "minimal": None}
    elif "gpt-5" in normalized_id:
        # Older GPT-5 variants reject the Responses API's `none` value. Keep
        # Pi's established minimal/low/medium/high choices for these models.
        entry["reasoning"] = True
        entry["input"] = ["text", "image"]
        entry["thinkingLevelMap"] = {"off": None}
    if "image" in entry.get("input", ()):
        entry["inputLimits"] = _PI_IMAGE_INPUT_LIMITS
    return entry


# Gemini limits from Pi's catalog (https://pi.dev/api/models, `google-vertex`,
# which is how Databricks hosts Gemini; `gemini-3-pro-image` is only listed
# under `openrouter`). Keys are canonical ids: prefix-stripped, dots → dashes.
# A bare entry would otherwise get Pi's 128k context / 4k output defaults.
_GEMINI_TOKEN_LIMITS: dict[str, dict[str, int]] = {
    "gemini-4-argon": {"context": 1_048_576, "output": 65_536},
    "gemini-3-8-flash": {"context": 1_048_576, "output": 65_536},
    "gemini-3-7-flash": {"context": 1_048_576, "output": 65_536},
    "gemini-3-6-flash": {"context": 1_048_576, "output": 65_536},
    "gemini-3-5-flash": {"context": 1_048_576, "output": 65_536},
    "gemini-3-5-flash-lite": {"context": 1_048_576, "output": 65_536},
    "gemini-3-1-pro": {"context": 1_048_576, "output": 65_536},
    "gemini-3-1-flash-lite": {"context": 1_048_576, "output": 65_536},
    "gemini-3-flash": {"context": 1_048_576, "output": 65_536},
    "gemini-2-5-pro": {"context": 1_048_576, "output": 65_536},
    "gemini-2-5-flash": {"context": 1_048_576, "output": 65_536},
    "gemini-2-5-flash-lite": {"context": 1_048_576, "output": 65_536},
    "gemini-3-pro-image": {"context": 65_536, "output": 32_768},
}
# Uncatalogued Gemini text models get the 1M/64k window every catalogued
# Gemini 2.5+ text model shares. Uncatalogued image-generation variants
# (`-image`, no tool calling or long context per the gateway listing) get the
# smaller window of the one catalogued image model instead.
_GEMINI_DEFAULT_LIMITS = {"context": 1_048_576, "output": 65_536}
_GEMINI_IMAGE_DEFAULT_LIMITS = {"context": 65_536, "output": 32_768}

# Pi thinking levels per Gemini model, from the Databricks reasoning-models
# doc, then Pi's catalog for models the doc does not list. `None` as a value
# means reasoning with no map (every Pi level through `high`).
#
# How Pi's google-generative-ai API sends levels (pi-ai `google-shared.ts`):
# ids matching `gemini-3(.N)-(pro|flash)` or `gemini-[4-9]` send
# `thinkingConfig.thinkingLevel` (MINIMAL/LOW/MEDIUM/HIGH); any other id sends
# `thinkingBudget` (-1 = dynamic for every level, 0 for off). Databricks'
# dashed ids (`gemini-3-5-flash`) miss the first regex, so only Argon,
# `gemini-3-flash` and `gemini-3-pro-image` take the level path today.
# Unmapped `xhigh`/`max` are hidden. With `off: None`, a thinking-disabled
# request on the level path is clamped to the lowest supported level.
_GEMINI_THINKING_LEVEL_MAPS: dict[str, dict[str, str | None] | None] = {
    # Always reasons; accepts low/medium (default)/high only. Both `minimal` and
    # `thinkingBudget` (Pi's off path) are rejected with HTTP 400, so off is
    # hidden and a disabled request clamps to `low`.
    "gemini-4-argon": {"off": None, "minimal": None},
    # Hybrid: low/medium (default)/high; can be off; `minimal` rejected.
    "gemini-3-8-flash": {"minimal": None},
    "gemini-3-7-flash": {"minimal": None},
    # Gemini 3 family: minimal/low (default)/medium/high; no off.
    "gemini-3-6-flash": {"off": None},
    "gemini-3-5-flash": {"off": None},
    "gemini-3-5-flash-lite": {"off": None},
    "gemini-3-1-flash-lite": {"off": None},
    "gemini-3-flash": {"off": None},
    # The doc lists `minimal` for 3.1 Pro, but the gateway rejects it ("thinking_level
    # MINIMAL is not supported by this model", HTTP 400) and Pi's catalog hides it.
    "gemini-3-1-pro": {"off": None, "minimal": None},
    # Not in the doc; Pi's catalog marks these reasoning with no level map.
    "gemini-2-5-pro": None,
    "gemini-2-5-flash": None,
    "gemini-2-5-flash-lite": None,
}


def _canonical_gemini_id(model_id: str) -> str:
    normalized_id = model_id.rsplit("/", 1)[-1].lower()
    for prefix in ("system.ai.", "databricks-"):
        if normalized_id.startswith(prefix):
            normalized_id = normalized_id[len(prefix) :]
            break
    return normalized_id.replace(".", "-")


def _pi_gemini_model_entry(model_id: str, spec: dict[str, object] | None = None) -> dict:
    """Build a Pi Gemini entry with explicit limits and thinking levels.

    A discovered context window (``spec["context_window"]``) overrides the
    static one. Models outside the doc and Pi's catalog get limits only, so Pi
    sends no ``thinkingConfig`` and the gateway applies the model default.
    """
    canonical_id = _canonical_gemini_id(model_id)
    static_limits = _GEMINI_TOKEN_LIMITS.get(canonical_id)
    if static_limits is None:
        static_limits = (
            _GEMINI_IMAGE_DEFAULT_LIMITS if "-image" in canonical_id else _GEMINI_DEFAULT_LIMITS
        )
    discovered_context = (
        _positive_int(spec.get("context_window")) if isinstance(spec, dict) else None
    )
    entry: dict = {
        "id": model_id,
        "contextWindow": discovered_context or static_limits["context"],
        "maxTokens": static_limits["output"],
    }
    if canonical_id in _GEMINI_THINKING_LEVEL_MAPS:
        entry["reasoning"] = True
        entry["input"] = ["text", "image"]
        entry["inputLimits"] = _PI_IMAGE_INPUT_LIMITS
        level_map = _GEMINI_THINKING_LEVEL_MAPS[canonical_id]
        if level_map is not None:
            entry["thinkingLevelMap"] = dict(level_map)
    return entry


def render_overlay(
    model: str,
    api_key: str,
    pi_base_urls: dict[str, str],
    claude_models: dict[str, str],
    codex_models: list[str],
    gemini_models: list[str],
    oss_models: list[str],
    oss_specs: list[dict] | None = None,
    claude_model_ids: list[str] | None = None,
    codex_specs: list[dict] | None = None,
    gemini_specs: list[dict] | None = None,
) -> tuple[dict, list[list[str]]]:
    """Return (overlay, managed_key_paths) for Pi's user agent config.

    ``api_key`` is a pi config value, not necessarily a literal bearer: see
    ``build_pi_api_key`` for the `!command` form every provider gets."""
    providers: dict = {}
    keys: list[list[str]] = [["model"]]
    # Pi expands header values that match an env var name. Our UA contains
    # `/` and a space so it can never collide — safe to pass as a literal.
    ua_headers = {"User-Agent": ucode_user_agent(ug_version(), f"pi/{agent_version('pi')}")}
    request_tags = request_tags_header_value()
    if request_tags:
        # Similarly to UA, request_tags is a JSON object (validated in `request_tags_header_value()`)
        # so it can never collide with an env var name
        ua_headers[AI_GATEWAY_REQUEST_TAGS_HEADER] = request_tags
    gateway_url = next(iter(pi_base_urls.values()), None)
    if gateway_url:
        ua_headers.update(workspace_headers(gateway_url))

    claude_ids = sorted(
        m
        for m in set(claude_models.values()) | set(claude_model_ids or [])
        if _pi_supports_claude_model(m)
    )
    if claude_ids:
        providers["databricks-claude"] = {
            "baseUrl": pi_base_urls["claude"],
            "api": "anthropic-messages",
            "apiKey": api_key,
            "authHeader": True,
            # Gateway's Anthropic translator rejects per-tool
            # `eager_input_streaming` on the streaming + tools path. Pi sends
            # the legacy beta header instead when this is false. Session
            # affinity keeps a Pi conversation on one AI Gateway destination,
            # which is important when traffic splitting is configured because
            # prompt caches are destination-local.
            "compat": {
                "supportsEagerToolInputStreaming": False,
                "sendSessionAffinityHeaders": True,
            },
            "headers": ua_headers,
            "models": [_pi_claude_model_entry(m) for m in claude_ids],
        }
        keys.append(["providers", "databricks-claude"])
    if codex_models:
        codex_specs_by_id = _responses_specs_by_id(codex_specs)
        providers["databricks-openai"] = {
            "baseUrl": pi_base_urls["openai"],
            "api": "openai-responses",
            "apiKey": api_key,
            "authHeader": True,
            "headers": ua_headers,
            "models": [
                _pi_gpt_model_entry(model_id, codex_specs_by_id.get(model_id))
                for model_id in codex_models
            ],
        }
        keys.append(["providers", "databricks-openai"])
    if gemini_models:
        gemini_specs_by_id = _responses_specs_by_id(gemini_specs)
        providers["databricks-gemini"] = {
            "baseUrl": pi_base_urls["gemini"],
            "api": "google-generative-ai",
            "apiKey": api_key,
            "authHeader": True,
            "headers": ua_headers,
            "models": [
                _pi_gemini_model_entry(model_id, gemini_specs_by_id.get(model_id))
                for model_id in gemini_models
            ],
        }
        keys.append(["providers", "databricks-gemini"])
    if oss_models:
        specs_by_id = _oss_specs_by_id(oss_specs)
        providers["databricks-mlflow"] = {
            "baseUrl": pi_base_urls["oss"],
            "api": "openai-completions",
            "apiKey": api_key,
            "authHeader": True,
            # MLflow chat-completions gateway rejects OpenAI's `store` field,
            # per-tool `strict`, and the `prompt_cache_retention` /
            # `prompt_cache_key` pair Pi adds under long cache retention (which
            # `build_runtime_env` turns on). Pi omits each when these are false.
            # Qwen, gpt-oss, Gemma and Llama also reject `max_completion_tokens`
            # (`json: unknown field`); every MLflow model accepts `max_tokens`.
            # OSS chat templates (e.g. Qwen 3.5) reject the `developer` role
            # ("Unexpected message role"); `system` is the safe default.
            "compat": {
                "supportsStore": False,
                "supportsStrictMode": False,
                "supportsLongCacheRetention": False,
                "maxTokensField": "max_tokens",
                "supportsDeveloperRole": False,
            },
            "headers": ua_headers,
            "models": [_pi_oss_model_entry(m, specs_by_id.get(m)) for m in oss_models],
        }
        keys.append(["providers", "databricks-mlflow"])
    overlay: dict = {
        "model": _resolve_model_selector(
            model,
            claude_models,
            codex_models,
            gemini_models,
            oss_models,
            claude_model_ids,
        ),
    }
    if providers:
        overlay["providers"] = providers
    return overlay, keys


def build_pi_api_key(state: dict) -> str:
    """Return the `!command` apiKey value pi resolves before every request.

    Pi runs a leading-`!` config value as a command and uses its stdout, and it
    resolves the provider apiKey per provider request rather than once per
    process, so the token is minted on demand and never lands in the config.

    No `--force-refresh`: pi has no token cache of its own on this path, so
    forcing a mint would round-trip to the workspace every turn. Pi also kills
    the command after 10s and discards its stderr, so plain `auth-token` serves
    ucode's own token cache (`ucode.token_cache`, answered by `ucode.entrypoint`
    before the full CLI imports) and bounds any CLI call it does make — see
    `AUTH_TOKEN_CLI_BUDGET_SECONDS`.

    Always POSIX-quoted: pi runs `!command` values through bash on every OS
    (Git Bash on Windows), which strips the backslashes from a cmd.exe-style
    ``C:\\...\\ug.exe`` path."""
    argv = build_auth_token_argv(
        state["workspace"],
        state.get("profile"),
        use_pat=bool(state.get("use_pat")),
    )
    return "!" + shlex.join(argv)


def write_tool_config(
    state: dict,
    model: str,
    token: str | None = None,
) -> tuple[dict, str]:
    with _CONFIG_WRITE_LOCK:
        return _write_tool_config_unlocked(state, model, token)


def _write_tool_config_unlocked(
    state: dict,
    model: str,
    token: str | None = None,
) -> tuple[dict, str]:
    backup_existing_file(PI_CONFIG_PATH, PI_BACKUP_PATH)
    if token is None:
        token = get_databricks_token(state["workspace"], state.get("profile"))
    pi_base_urls = state.get("base_urls", {}).get("pi") or build_pi_base_urls(state["workspace"])
    managed_families = _managed_model_families(state)
    claude_model_ids: list[str] | None = None
    if managed_families is None:
        claude_models = state.get("claude_models") or {}
        codex_models = state.get("codex_models") or []
        gemini_models = state.get("gemini_models") or []
        oss_models = state.get("oss_models") or []
        # The shared map keeps one model per family; Pi supplements it with
        # the full inventory for every family that discovery enabled.
        claude_model_ids = (
            _discover_pi_claude_models(state, token, claude_models) if claude_models else None
        )
    else:
        claude_models, codex_models, gemini_models, oss_models = managed_families
        claude_model_ids = _managed_pi_claude_models(state)
    overlay, managed_keys = render_overlay(
        model,
        build_pi_api_key(state),
        pi_base_urls,
        claude_models,
        codex_models,
        gemini_models,
        oss_models,
        state.get("oss_model_specs") or [],
        claude_model_ids,
        state.get("codex_model_specs") or [],
        state.get("gemini_model_specs") or [],
    )
    existing = read_json_safe(PI_CONFIG_PATH)
    providers = existing.get("providers")
    if isinstance(providers, dict):
        for stale in (*PROVIDER_NAMES, *LEGACY_PROVIDER_NAMES):
            providers.pop(stale, None)
    merged = deep_merge_dict(existing, overlay)
    write_json_file(PI_CONFIG_PATH, merged)
    _write_settings(overlay["model"], clear_unresolved=managed_families is not None)
    state = mark_tool_managed(state, "pi", managed_keys)
    save_state(state)
    return state, token


def _managed_pi_claude_models(state: dict) -> list[str]:
    """Return every Claude id explicitly allowed by a managed Pi config."""
    managed = state.get("pi_models")
    if not isinstance(managed, list):
        return []
    return [
        model
        for model in managed
        if isinstance(model, str) and classify_model_family(model) in ANTHROPIC_FAMILIES
    ]


def _discover_pi_claude_models(state: dict, token: str, claude_models: dict[str, str]) -> list[str]:
    """Return all Claude ids Pi may offer without changing shared routing state.

    Shared discovery intentionally keeps one model per Claude family and pins
    Opus to 4.8 while the smart router requires that arm. Pi is a model picker,
    so it can safely expose newer versions as long as its default remains the
    shared pinned id.
    Cache the supplemental inventory in state after the first Pi config write;
    a failed supplemental request degrades to the shared family picks.
    """
    allowed_families = set(claude_models)
    cached = state.get("pi_claude_models")
    if isinstance(cached, list):
        cached_models = [
            model
            for model in cached
            if isinstance(model, str) and classify_model_family(model) in allowed_families
        ]
        return list(dict.fromkeys([*claude_models.values(), *cached_models]))

    try:
        discovered, _ = discover_claude_models_unbucketed(state["workspace"], token)
    except (RuntimeError, OSError):
        discovered = []
    if discovered:
        state["pi_claude_models"] = discovered
        discovered_models = [
            model for model in discovered if classify_model_family(model) in allowed_families
        ]
        return list(dict.fromkeys([*claude_models.values(), *discovered_models]))
    return list(claude_models.values())


def _write_settings(model_selector: str, *, clear_unresolved: bool = False) -> None:
    # Pin defaultProvider/defaultModel in settings.json so Pi doesn't fall
    # through to an env-key-backed provider (e.g. HF_TOKEN exposing
    # huggingface) in `findInitialModel` when no --model is passed.
    provider, _, model_id = model_selector.partition("/")
    if not model_id and not clear_unresolved:
        return
    backup_existing_file(PI_SETTINGS_PATH, PI_SETTINGS_BACKUP_PATH)
    existing = read_json_safe(PI_SETTINGS_PATH)
    if not model_id:
        # A non-empty managed allowlist with no servable model must not retain a
        # stale default that bypasses the current administrator policy.
        existing.pop("defaultProvider", None)
        existing.pop("defaultModel", None)
        write_json_file(PI_SETTINGS_PATH, existing)
        return
    merged = deep_merge_dict(existing, {"defaultProvider": provider, "defaultModel": model_id})
    write_json_file(PI_SETTINGS_PATH, merged)


def _managed_model_families(
    state: dict,
) -> tuple[dict[str, str], list[str], list[str], list[str]] | None:
    """Split a managed config's ``pi_models`` into Pi provider inputs.

    ``None`` means there is no managed allowlist and workspace discovery may be
    used. A present non-empty allowlist always returns four collections—even if
    every entry is malformed or unsupported—so unlisted discovered models can
    never leak back into managed Pi configuration.
    """
    managed = state.get("pi_models")
    if not isinstance(managed, list) or not managed:
        return None
    claude: dict[str, str] = {}
    codex: list[str] = []
    gemini: list[str] = []
    oss: list[str] = []
    for model in managed:
        if not isinstance(model, str) or not model.strip():
            continue
        family = classify_model_family(model)
        if family in ANTHROPIC_FAMILIES:
            claude.setdefault(family, model)
        elif family == "codex":
            codex.append(model)
        elif family == "gemini":
            gemini.append(model)
        elif family == "oss":
            oss.append(model)
    return claude, codex, gemini, oss


DEFAULT_MODEL = "system.ai.gpt-6-luna"


def default_model(state: dict) -> str | None:
    """Prefer a managed Pi default/allowlist, then the Unity Gateway default.

    A managed config's ``pi_default_model`` and ``pi_models`` both win outright: the former is
    the admin's chosen session start, the latter their allowlist. Workspace-wide discovery falls back.
    """
    if isinstance(state.get("pi_default_model"), str):
        return state.get("pi_default_model")
    managed = state.get("pi_models")
    if isinstance(managed, list) and managed:
        return managed[0]
    claude_models = state.get("claude_models") or {}
    for family in ("opus", "sonnet", "haiku"):
        if claude_models.get(family):
            return claude_models[family]
    codex_model = preferred_gpt_model(state.get("codex_models") or [])
    if codex_model:
        return codex_model
    gemini_models = state.get("gemini_models") or []
    if gemini_models:
        return gemini_models[0]
    oss_models = state.get("oss_models") or []
    if oss_models:
        return oss_models[0]
    return next(iter(claude_models.values()), DEFAULT_MODEL)


def _configure_launch(state: dict) -> str:
    model = default_model(state)
    if not model:
        raise RuntimeError("No Pi model is available on this workspace.")
    _, token = write_tool_config(state, model)
    return token


# Pi's default ("short") sends no retention hint, leaving OpenAI on its
# in-memory prompt cache (~5-10 min idle) and Anthropic on the 5m TTL. "long"
# requests `prompt_cache_retention: "24h"` (Responses) and `cache_control.ttl:
# "1h"` (Anthropic); both verified accepted by AI Gateway. MLflow opts out via
# `supportsLongCacheRetention: false` in its provider compat.
PI_CACHE_RETENTION_DEFAULT = "long"


def build_runtime_env(token: str) -> dict[str, str]:
    env = os.environ.copy()
    env["OAUTH_TOKEN"] = token
    env["PI_CODING_AGENT_DIR"] = str(PI_CONFIG_DIR)
    # An explicit PI_CACHE_RETENTION (e.g. `short` to avoid Anthropic's 2x
    # 1h cache-write price) wins.
    if not env.get("PI_CACHE_RETENTION", "").strip():
        env["PI_CACHE_RETENTION"] = PI_CACHE_RETENTION_DEFAULT
    # Pi's databricks-gemini provider must hit the gateway's Gemini route
    # (`/v1beta/models/<id>:streamGenerateContent`). With Vertex settings
    # inherited from the shell (often exported for unrelated tools), Pi's
    # @google/genai SDK switches to Vertex `publishers/google/models/...`
    # paths, which the gateway rejects. Pin the SDK to the Gemini API.
    env["GOOGLE_GENAI_USE_VERTEXAI"] = "false"
    for name in _VERTEX_PROJECT_ENV_VARS:
        env.pop(name, None)
    return env


_VERTEX_PROJECT_ENV_VARS = ("GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION")


def _is_loopback_origin(origin: str) -> bool:
    hostname = urlparse(origin).hostname
    if not hostname:
        return True
    if hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def _start_oss_proxy(
    state: dict,
) -> tuple[threading.Thread, _mlflow_proxy.ThreadingHTTPServer] | None:
    """Start Pi's MLflow repair proxy and rewrite only the in-memory base URL.

    The upstream is always derived from ``workspace`` rather than an existing
    base URL, so a stale loopback port from an old config can never become the
    next proxy's upstream.
    """
    managed_families = _managed_model_families(state)
    oss_models = state.get("oss_models") or [] if managed_families is None else managed_families[3]
    if not oss_models:
        return None
    direct_oss_url = build_pi_base_urls(state["workspace"])["oss"]
    origin = direct_oss_url.split("/ai-gateway/", 1)[0]
    if _is_loopback_origin(origin):
        print_warning("MLflow stream repair proxy skipped for a loopback workspace URL.")
        return None
    started = _mlflow_proxy.start(origin)
    if started is None:
        return None
    server, proxy_origin = started
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    try:
        thread.start()
    except RuntimeError:
        server.server_close()
        print_warning("MLflow stream repair proxy could not start; using the direct gateway URL.")
        return None
    pi_urls = state.setdefault("base_urls", {}).setdefault(
        "pi", build_pi_base_urls(state["workspace"])
    )
    pi_urls["oss"] = f"{proxy_origin}/ai-gateway/mlflow/v1"
    return thread, server


def _restore_direct_oss_config(state: dict, token: str | None) -> None:
    """Replace the session-only proxy URL after any in-flight config write."""
    with _CONFIG_WRITE_LOCK:
        _restore_direct_oss_config_unlocked(state, token)


def _restore_direct_oss_config_unlocked(state: dict, token: str | None) -> None:
    pi_urls = state.setdefault("base_urls", {}).setdefault(
        "pi", build_pi_base_urls(state["workspace"])
    )
    pi_urls["oss"] = build_pi_base_urls(state["workspace"])["oss"]
    model = default_model(state)
    if model and token is not None:
        write_tool_config(state, model, token=token)
        return

    # Token acquisition can fail before the normal config rewrite returns a
    # token. Repair an existing generated provider in place without changing
    # its credential, so a stale loopback port is never left behind.
    existing = read_json_safe(PI_CONFIG_PATH)
    providers = existing.get("providers")
    mlflow = providers.get("databricks-mlflow") if isinstance(providers, dict) else None
    if isinstance(mlflow, dict):
        mlflow["baseUrl"] = pi_urls["oss"]
        write_json_file(PI_CONFIG_PATH, existing)


def launch(state: dict, tool_args: list[str], *, options: LaunchOptions) -> None:
    """Launch Pi behind the MLflow stream-repair proxy.

    Pi re-resolves each provider's `!command` apiKey per request, so no token
    refresher thread is needed; only the proxy needs lifecycle management.
    """
    proxy: tuple[threading.Thread, _mlflow_proxy.ThreadingHTTPServer] | None = None
    token: str | None = None
    primary_error: BaseException | None = None
    try:
        # The proxy must be live and its URL in state before the first config
        # write; later writes then keep the same live loopback endpoint.
        proxy = _start_oss_proxy(state)
        token = _configure_launch(state)
        env = build_runtime_env(token)

        proc = subprocess_cross_os.popen([SPEC["binary"], *tool_args], env=env)
        try:
            returncode = proc.wait()
        except KeyboardInterrupt:
            proc.send_signal(signal.SIGINT)
            returncode = proc.wait()
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if proxy is not None:
            proxy_thread, server = proxy
            restore_error: Exception | None = None
            try:
                _restore_direct_oss_config(state, token)
            except Exception as exc:
                restore_error = exc
                print_warning(f"Pi MLflow direct configuration could not be restored ({exc}).")
            finally:
                server.shutdown()
                server.server_close()
                proxy_thread.join(timeout=1)
            if restore_error is not None and primary_error is None:
                raise restore_error

    raise SystemExit(returncode)


def validate_cmd(binary: str) -> list[str]:
    return [binary, "--print", "say hi in 5 words or less"]
