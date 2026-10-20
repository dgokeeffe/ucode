"""Shared fixtures for E2E tests + global state-isolation guard."""

from __future__ import annotations

import os
from types import SimpleNamespace

import pytest

from ucode.databricks import (
    build_shared_base_urls,
    discover_claude_models,
    discover_codex_models,
    discover_gemini_models,
    discover_model_services,
    discover_oss_model_specs,
    get_databricks_token,
)
from ucode.ui import normalize_workspace_url

# The integration suite has its own configuration and subprocess-only fixtures.
# Run it through scripts/run_integration.py, outside this fixture hierarchy.
collect_ignore = ["integration", "e2e_cuj"]


@pytest.fixture(autouse=True)
def _isolate_ucode_state(tmp_path, monkeypatch):
    """Redirect ucode's state file and APP_DIR to a per-test tmp dir.

    Defense in depth: even if an individual test forgets to patch save_state,
    it can never touch the developer's real ~/.ucode/state.json or invoke the
    privileged writer for an OS-managed agent config.
    """
    import ucode.auth_token_log as auth_token_log_mod
    import ucode.codex_config as codex_config_mod
    import ucode.config_io as config_io_mod
    import ucode.databricks as databricks_mod
    import ucode.managed_config as managed_config_mod
    import ucode.managed_files as managed_files_mod
    import ucode.os_compatibility.subprocess_cross_os as subprocess_cross_os_mod
    import ucode.state as state_mod
    import ucode.token_cache as token_cache_mod
    from ucode.agents import codex as codex_mod

    state_dir = tmp_path / ".ucode"
    state_dir.mkdir()
    monkeypatch.setattr(token_cache_mod, "CACHE_DIR", state_dir / "token-cache")
    monkeypatch.setattr(auth_token_log_mod, "LOG_PATH", state_dir / "auth-token-errors.log")
    # A real background refresher is a separate process that sees none of these patches: it
    # would run the developer's Databricks CLI and write their real ~/.ucode token cache.
    monkeypatch.setattr(token_cache_mod, "_spawn_detached", lambda argv: None)
    monkeypatch.setattr(state_mod, "STATE_PATH", state_dir / "state.json")
    monkeypatch.setattr(config_io_mod, "APP_DIR", state_dir)
    # MANAGED_CONFIG_PATH is bound from APP_DIR at import, so patching APP_DIR alone doesn't move it;
    # rebind it or save_managed_state writes to the developer's real ~/.ucode/managed-config.json.
    monkeypatch.setattr(
        managed_config_mod, "MANAGED_CONFIG_PATH", state_dir / "managed-config.json"
    )
    backup_dir = state_dir / "managed-backups"
    monkeypatch.setattr(managed_files_mod, "MANAGED_BACKUP_DIR", backup_dir)
    monkeypatch.setattr(
        managed_files_mod, "MANAGED_BACKUP_MANIFEST_PATH", backup_dir / "manifest.json"
    )
    monkeypatch.setattr(codex_mod, "codex_managed_config_path", lambda: None)
    monkeypatch.setattr(
        codex_mod, "CODEX_MODEL_CATALOG_PATH", state_dir / "codex-model-catalog.json"
    )
    monkeypatch.setattr(codex_mod, "CODEX_CONFIG_PATH", tmp_path / ".codex" / "ucode.config.toml")
    # `custom_catalog_path` resolves Codex config precedence through *this* module's own
    # globals, so patching the `codex` agent's copies above does not redirect it. Without
    # these, the suite reads the developer's real ~/.codex/config.toml and any
    # `model_catalog_json` it points at, letting a personal catalog decide the model a
    # smart-routing test asserts on.
    monkeypatch.setattr(codex_config_mod, "codex_managed_config_path", lambda: None)
    monkeypatch.setattr(
        codex_config_mod,
        "DEFAULT_CODEX_CONFIG_PATH",
        tmp_path / ".codex" / "ucode.config.toml",
    )
    # CODEX_HOME overrides the profile path inside `codex_config_precedence_paths`, so a
    # developer's ambient value would reintroduce the same leak.
    monkeypatch.delenv("CODEX_HOME", raising=False)

    def reject_privileged_write(path, _desired_text):
        pytest.fail(
            f"test attempted a privileged managed-config write to {path}; "
            "mock the agent's managed path and writer"
        )

    monkeypatch.setattr(managed_files_mod, "_sudo_replace", reject_privileged_write)
    monkeypatch.delenv("ENABLE_CLAUDE_CODE_GATEWAY_MODEL_DISCOVERY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY", raising=False)
    monkeypatch.delenv("UCODE_SESSION_ENV_FILE", raising=False)
    monkeypatch.delenv("UCODE_SMART_ROUTER_PYTHON", raising=False)
    # When the suite runs inside a ug smart-routing session, these ambient env vars
    # make smart_routing_enabled()/configured_router_name() read the host's live
    # routing state, leaking a Databricks-Smart-Router-Recipe header into
    # header-rendering tests. Clear them so routing stays off unless a test opts in.
    monkeypatch.delenv("ENABLE_SMART_ROUTING_V2", raising=False)
    monkeypatch.delenv("ENABLE_SMART_ROUTING_SUBAGENT_ONLY", raising=False)
    monkeypatch.delenv("SMART_ROUTER_NAME", raising=False)
    # On Windows, resolve_command swaps a bare program name for whatever `shutil.which`
    # finds on the developer's PATH (e.g. a real `codex.CMD`). Rebind only the compatibility
    # helper's `shutil` so argv stays host-independent; helper tests patch `which` explicitly.
    monkeypatch.setattr(
        subprocess_cross_os_mod, "shutil", SimpleNamespace(which=lambda _name: None)
    )
    # A developer's ambient managed-config stub would otherwise short-circuit every fetch in the suite.
    monkeypatch.delenv("UCODE_MANAGED_CONFIG_STUB", raising=False)
    # The model-services listing is memoized for the life of the process, so without this a cached
    # result would leak into the next test and make a stubbed listing look like it was never called.
    databricks_mod.clear_model_services_cache()
    databricks_mod.clear_workspace_org_id_cache()
    # The resolved Databricks CLI path/discovery is cached for the life of the
    # process; a real resolution (or a prior test's patched one) must not leak
    # into a test that assumes a bare "databricks" or a specific fake CLI.
    databricks_mod.clear_databricks_cli_cache()


def _workspace() -> str:
    ws = os.environ.get("UCODE_TEST_WORKSPACE", "").strip().rstrip("/")
    return normalize_workspace_url(ws) if ws else ""


@pytest.fixture(scope="session")
def e2e_workspace():
    ws = _workspace()
    if not ws:
        pytest.skip("Set UCODE_TEST_WORKSPACE=https://... to run E2E tests")
    return ws


@pytest.fixture(scope="session")
def e2e_token(e2e_workspace):
    return get_databricks_token(e2e_workspace)


@pytest.fixture(scope="session")
def e2e_state(e2e_workspace, e2e_token):
    """Full state dict mirroring configure's UC-first family discovery."""
    claude_models, codex_models, gemini_models, oss_models, _ = discover_model_services(
        e2e_workspace, e2e_token
    )
    if not claude_models:
        claude_models, _ = discover_claude_models(e2e_workspace, e2e_token)
    if not gemini_models:
        gemini_models, _ = discover_gemini_models(e2e_workspace, e2e_token)
    if not codex_models:
        codex_models, _ = discover_codex_models(e2e_workspace, e2e_token)
    if oss_models:
        oss_model_specs, _ = discover_oss_model_specs(e2e_workspace, e2e_token, oss_models)
    else:
        oss_model_specs, _ = discover_oss_model_specs(e2e_workspace, e2e_token)
    oss_models = [spec["id"] for spec in oss_model_specs]

    # E2E mirrors configure's default (Fable is premium and opt-in).
    claude_models.pop("fable", None)

    opencode_models: dict = {}
    if claude_models:
        opencode_models["anthropic"] = list(claude_models.values())
    if gemini_models:
        opencode_models["gemini"] = gemini_models
    if codex_models:
        opencode_models["openai"] = codex_models
    if oss_models:
        opencode_models["oss"] = oss_models

    return {
        "workspace": e2e_workspace,
        "claude_models": claude_models,
        "gemini_models": gemini_models,
        "codex_models": codex_models,
        "oss_models": oss_models,
        "oss_model_specs": oss_model_specs,
        "opencode_models": opencode_models,
        "base_urls": build_shared_base_urls(e2e_workspace),
        "managed_configs": {},
    }
