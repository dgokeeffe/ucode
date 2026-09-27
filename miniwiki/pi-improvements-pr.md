# Plan: consolidated Pi improvements PR to databricks/ucode

Status: planned, not started. No branch pushed, no PR opened. Opening/pushing remains an explicit human action.

## Objective

Land the consolidated, tested Pi work currently on local `dev` as **one upstream PR** against `databricks/ucode:main`, scoped to Pi behavior plus the shared discovery/proxy code Pi depends on. Older PRs (#217, #223, #333, #334, #335, #336) become superseded references rather than the review surface.

## Current state (measured)

- `git rev-list --left-right --count origin/main...dev` → **15 ahead / 32 behind**. `dev` must be re-synced with `origin/main` before any PR branch is cut.
- `origin/main` head: `d16bf16` (`Add ucode doctor (#294)`).
- Main has advanced with: smart routing v2 legacy cleanup (#434), `ucode doctor` (#294), Codex OS-scope settings (#416), Claude 2.1.248 requirement (#413/#441), Claude OS-managed settings (#415), usage summary redesign (#424), MPS servable default (#380).
- Main does **not** yet contain the Grok work or the native Responses route: `git grep -i grok origin/main -- src/ucode/{databricks.py,agents/pi.py}` is empty, and `ai-gateway/openai/v1` is absent from `origin/main:src/ucode`.
- `src/ucode/agents/pi.py`: 317 lines on main, 730 on dev.
- `pyproject.toml` is **unchanged** between main and dev, so the 2572-line `uv.lock` churn is unrelated drift and must not ship in the PR.

## 2026-09-02 main sync (steps 1–2 complete)

`origin/main` at `1b0883b` (`only require 2.1.248 for smart routing + model discovery (#441)`) was merged into `dev` as `f57da3b`.

- Backup branch before the sync: `backup/dev-before-pr-sync-20260902-093513`. The worktree was clean apart from this untracked plan page, so no stash was required.
- `dev` is now **33 ahead / 0 behind** `origin/main`; `git merge-base --is-ancestor origin/main dev` passes.
- Only two conflicts, both resolved deliberately:
  - `src/ucode/agents/pi.py`: dropped the local `available_npm_package_update` import. Main centralized the npm update check in `agents/__init__.py` (`#294`/`#415`), so Pi's own copy was dead code. Kept the `_mlflow_proxy` import.
  - `tests/test_state.py`: took main's Codex expectation (`"model" not in persisted["agents"]["codex"]`, from #429/#431's harness-default behavior) **and** kept dev's Pi latest-numeric-GPT expectation (`system.ai.gpt-5-6-luna`). The stale "both GPT-capable launchers" comment was corrected. This is the union, not a side choice: Codex defers to its harness default while Pi still needs newest-model selection.
- The feared #434 legacy-routing conflict did not materialize; `git merge` auto-resolved `databricks.py`, `cli.py`, `opencode.py`, `claude.py`, and `managed_resolve.py`.

Verification evidence for the sync:

- `uv run --frozen pytest -q -k 'not TestStateFileIsNotRewritten and not test_user_agent_arrives_at_gateway'`: **2365 passed, 37 skipped, 13 deselected in 121.58s**, with only the two baseline smart-routing socket failures.
- Baseline adjudication: `tests/test_claude_smart_routing_v2.py::TestFirstPromptHook::test_blocks_once_then_allows_replay` and `::TestPtyFlow::test_direct_switch_restore_and_replay` fail identically in a detached `origin/main` worktree (**2 failed, 20 passed in 5.63s**) with the same "could not start its local prompt-routing socket" error. Environmental, not a merge regression.
- Focused: `uv run --frozen pytest -q tests/test_state.py tests/test_agent_pi.py`: **123 passed in 18.71s**.
- `uv run --frozen ruff check .`: passed. `uv run --frozen ruff format --check src/ tests/`: **96 files already formatted**. `uv run --frozen ty check src`: passed.
- `git diff --check origin/main...dev`: clean. Conflict-marker scan of `src/` and `tests/`: clean.

Next action is step 3 onward: cut `feat/pi-consolidated-improvements` from this synced `dev`, restore `uv.lock` to main's version, and strip the excluded paths.

## Diff surface at the time of planning (`git diff --stat origin/main...dev`)

Ship:

- `src/ucode/agents/pi.py`, `src/ucode/agents/_mlflow_proxy.py` (new), `src/ucode/agents/__init__.py`
- `src/ucode/databricks.py`, `src/ucode/managed_resolve.py`, `src/ucode/cli.py`
- `src/ucode/agents/opencode.py`, `src/ucode/agents/claude.py` (only the shared-policy parts Pi requires)
- Tests: `tests/test_agent_pi.py`, `tests/test_mlflow_proxy.py`, `tests/test_databricks.py`, `tests/test_cli.py`, `tests/test_agent_opencode.py`, `tests/test_agent_claude.py`, `tests/test_agents_init.py`, `tests/conftest.py`, `tests/test_state.py`, `tests/test_managed_resolve.py`, `tests/test_e2e.py`, `tests/test_e2e_user_agent.py`

Exclude:

- `uv.lock` (no `pyproject.toml` change justifies it)
- `verification/*.yaml` (internal orchestration evidence; summarize in the PR body instead)
- `miniwiki/*.md` (internal working notes)
- `.gitignore` `.pi-subagents` entry (local tooling)
- `scripts/prompt_cache_http_trace.py`, `scripts/prompt_cache_repro.py` + their tests — hold for a follow-up diagnostics PR unless a reviewer asks for the cache evidence inline

## 2026-09-02 branches cut (steps 3–6 complete)

Both branches exist locally and are green. Neither is pushed; no PR is open.

- `pr/shared-model-discovery` — `2e2ed6b`, based on `origin/main` `1b0883b`. **12 files changed, +2537/-125.**
- `pr/pi-mlflow-oss-provider` — `61d1156`, stacked on `pr/shared-model-discovery` (ancestry verified). **10 files changed, +1932/-80.**
- Worktrees: `/tmp/ucode-pr1`, `/tmp/ucode-pr2`. `dev` at `f57da3b` was never checked out or modified.

### The boundary changed during execution, for the better

The plan called for splitting `pi.py` hunks across both PRs. That was unnecessary: inspection showed the native-route switch in `pi.py` is **only a docstring line**, because the real change lives in `build_pi_base_urls` in `databricks.py`. Likewise `main`'s `pi.py` never calls `gpt_model_token_limits` or `_pi_gpt_model_entry` — those are entirely new.

So the split is **file-level, with no intra-file surgery**:

- PR 1: `databricks.py`, `cli.py`, `agents/opencode.py`, `agents/claude.py` + `test_databricks`, `test_cli`, `test_agent_opencode`, `test_agent_claude`, `test_state`, `conftest`, `test_e2e`, `test_e2e_user_agent`.
- PR 2: `agents/pi.py`, `agents/_mlflow_proxy.py` (new), `agents/__init__.py`, `managed_resolve.py` + `test_agent_pi`, `test_mlflow_proxy`, `test_agents_init`, `test_managed_resolve`, and the PR2 halves of `test_cli`/`test_state`.

This also relocated Grok thinking levels and Pi's supplemental Claude discovery into PR 2, giving each PR one coherent theme: PR 1 is shared discovery/routing, PR 2 is everything Pi.

### Two real cross-boundary defects were caught by dry-running PR 1

A PR1-only test run failed 2 tests beyond the environmental baselines, both because PR1 test files asserted PR2 behavior:

- `tests/test_state.py::test_persists_latest_gpt_launcher_default_in_agent_state` asserted Pi's newest-numeric-GPT selection, which lives in `pi.py`. PR 1 keeps main's test name and `system.ai.gpt-5` expectation plus the `/ai-gateway/openai/v1` `FAKE_URLS` change; PR 2 restores dev's version.
- `tests/test_cli.py::test_active_listing_replaces_unlisted_gpt_in_state_and_overlays` (new in dev) called `pi_mod.render_overlay` with dev's 8-argument signature. PR 1 keeps the CLI/OpenCode assertions and drops the Pi overlay block; PR 2 restores it.

Had these branches been pushed without the dry run, PR 1 would have failed CI on its own.

### Verification evidence

PR 1 (`/tmp/ucode-pr1`):

- `uv run --frozen pytest -q -k 'not TestStateFileIsNotRewritten and not test_user_agent_arrives_at_gateway'`: **2287 passed, 37 skipped, 13 deselected in 85.25s**; only the 2 baseline smart-routing socket failures.
- `ruff check .`: passed. `ruff format --check src/ tests/`: **92 files already formatted**. `ty check src`: passed.
- Import smoke test (`ucode.cli`, `ucode.agents.pi`, `ucode.databricks`): OK — confirms main's `pi.py` still imports against the new `databricks.py`.

PR 2 (`/tmp/ucode-pr2`):

- Same bounded suite: **2354 passed, 37 skipped, 13 deselected in 117.25s**; only the same 2 baseline failures.
- `ruff check .`: passed. `ruff format --check src/ tests/`: **94 files already formatted**. `ty check src`: passed.

Stack integrity:

- `git diff --stat dev -- src/` from PR 2 tip is **empty**: PR1 + PR2 reproduces `dev`'s `src/` tree exactly. `tests/` differs only by the deferred `prompt_cache` diagnostics.
- `git merge-base --is-ancestor pr/shared-model-discovery pr/pi-mlflow-oss-provider`: passed.
- `git diff --stat origin/main..HEAD -- uv.lock pyproject.toml`: empty, so no lock churn ships.
- No commit in either branch touches `verification/`, `miniwiki/`, `prompt_cache*`, or `.gitignore`.
- `git diff --check origin/main..HEAD`: clean.

### Residual: commit granularity

Each PR is a single squashed commit authored from the verified final tree, not the 10-commit series the plan described. Reconstructing per-concern commits would have required cherry-picking ~20 interleaved dev commits across a moved file boundary, which is materially more error-prone than proving the final tree matches `dev`. If reviewers want a series, the source commits remain on `dev` and in `backup/dev-before-pr-sync-20260902-093513`.

### Still outstanding before review is requested

No live-workspace run has been done for either branch. PR 1 needs `ucode pi` against Claude and Grok; PR 2 needs a streaming run against an MLflow/OSS model.

## Revised shape: two stacked PRs (supersedes the single-PR plan below)

Coupling analysis of the merged tree shows the floor is **two PRs**, not one.

Why not one: the diff is ~7.5k lines and splits cleanly along two different risk profiles that want different review attention — cross-agent discovery regressions versus loopback-proxy network/streaming/lifecycle behavior.

Why not three or more: the discovery changes are mutually dependent. Capability classification, Grok exposure, the gateway-only fallback, and context-window parsing all edit the same functions in `databricks.py`. Splitting them manufactures conflicts and re-litigates the same review.

The intermediate state after PR 1 alone is safe, which is what makes the split legitimate: `origin/main` **already** persists `oss_models` (`cli.py:721`) and OpenCode already consumes them (`opencode.py:60,99,134`). Pi has no OSS provider on main. So PR 1 improves OpenCode/Codex model listing and routing while Pi simply does not yet gain an OSS provider. Nothing is half-wired.

### PR 1 — Model discovery, capability metadata, and native Responses routing

Files: `src/ucode/databricks.py`, `src/ucode/cli.py`, `src/ucode/agents/claude.py`, `src/ucode/agents/opencode.py`, plus the `src/ucode/agents/pi.py` hunks that only consume shared helpers. Tests: `test_databricks.py`, `test_cli.py`, `test_agent_opencode.py`, `test_agent_claude.py`, `test_state.py`, `test_e2e.py`, `test_e2e_user_agent.py`, `conftest.py`.

Content: capability-based classification of `system.ai.*`, Grok exposure, Claude inventory fallbacks, gateway-only model offering, context-window parsing into `codex_model_specs`, the shared Claude/GPT context policy, the native `/ai-gateway/openai/v1` route, GPT off-reasoning suppression, Grok thinking levels, and `PI_CODING_AGENT_DIR`.

Risk: **highest blast radius in the stack.** `databricks.py` is consumed by `managed_wizard.py`, `managed_setup.py`, and `smart_routing/codex_hooks.py`, none of which this PR touches. Mitigation: every new discovery entry point is additive (`discover_responses_model_specs`, `discover_oss_model_specs`, `discover_oss_models`) and is called only from `cli.py`; no existing shared signature changes arity. The one genuinely behavioral change is the route switch, and it must be exercised live.

### PR 2 — Pi MLflow OSS provider (stacked on PR 1)

Files: `src/ucode/agents/_mlflow_proxy.py` (new), `src/ucode/agents/__init__.py`, `src/ucode/managed_resolve.py`, and the OSS-provider / proxy-lifecycle / managed-allowlist hunks of `src/ucode/agents/pi.py` (around `pi.py:354` and `pi.py:604-666`). Tests: `test_mlflow_proxy.py`, `test_agent_pi.py`, `test_agents_init.py`, `test_managed_resolve.py`.

Content: the validated MLflow OSS provider, SSE termination repair, proxy truncation/shutdown safety, managed OSS allowlist enforcement, and managed OSS lifecycle/settings alignment.

Risk: **contained.** A new file plus Pi-only wiring; no other agent reads the proxy. The residual risks are proxy lifecycle (leaked thread or port on failure) and the allowlist invariant.

### Cost of splitting `pi.py` across both PRs

`pi.py` is +462/-54 across 17 hunks, but only ~28 added lines mention `mlflow` or `proxy`. The split point is therefore small and mechanical: the OSS `baseUrl` entry, the proxy start/stop lifecycle, and the managed OSS allowlist go to PR 2; the model-selector and capability logic (the +183-line hunk) stays in PR 1.

### Deferred out of both PRs

`scripts/prompt_cache_http_trace.py`, `scripts/prompt_cache_repro.py` and their tests (optional PR 3, diagnostics only); `uv.lock`; `verification/`; `miniwiki/`; the `.gitignore` entry.

### Live-workspace split

PR 1 needs a live `UCODE_TEST_WORKSPACE` run covering `ucode pi` on a Claude model and a Grok model (closes the native-Responses-route residual). PR 2 needs a live streaming run on an MLflow/OSS model (closes the SSE-termination residual).

## Original single-PR commit series (kept for commit ordering within the stack)

Readable commit series (no squash), in this order:

1. `fix: share Claude and GPT context capability policy` (#333 content) — shared `claude_model_supports_1m` / context metadata used by Pi, OpenCode, Claude.
2. `feat(discovery): classify system models by capability` — UC model-service IDs joined to the cached foundation-model catalog; Responses → OpenAI/Codex catalog, native Gemini → Gemini, MLflow-chat-only → OSS; embeddings/rerankers excluded; name fallbacks only when per-model metadata is missing.
3. `feat(discovery): discover OSS MLflow capabilities and model metadata` (#334 content), including persisted `codex_model_specs` context windows.
4. `fix(discovery): expose Grok in Pi model catalog` + `fix(discovery): harden Claude inventory fallbacks` + `fix(discovery): offer gateway models the UC listing hasn't registered yet`.
5. `fix(routing): use native Responses endpoint` — Pi/OpenCode use `/ai-gateway/openai/v1`; Codex CLI, Codex smart routing, and web search stay on `/ai-gateway/codex/v1`.
6. `feat(pi): add validated MLflow OSS provider` + `fix(pi): repair MLflow SSE stream termination` + `fix(pi): preserve proxy safety across truncation and shutdown`.
7. `fix(pi): enforce managed OSS model allowlists` + `fix(pi): align managed OSS lifecycle and settings`.
8. `fix(pi): expose supplemental Claude model versions`.
9. `fix(pi): omit unsupported GPT off-reasoning effort` (#223) + `fix(pi): expose Grok reasoning levels` (`low`/`medium`/`high`/`xhigh` only).
10. `fix(pi): use the standard user config directory` — only if `PI_CODING_AGENT_DIR` handling still differs from main after the sync.

Optional stacked follow-ups, not in this PR: prompt-cache diagnostics scripts (#336 content), and Pi session affinity if it proves separable.

## Invariants to preserve (assert in tests)

- Pi uses `PI_CODING_AGENT_DIR`; it never replaces `HOME`.
- A nonempty managed Pi model allowlist can never fall back to unlisted discovery results.
- Managed `pi_models` stays authoritative and keeps multiple versions within one Claude family.
- Fable stays opt-in; do not auto-include it.
- `gpt-oss` stays out of the Responses catalog; Grok stays out of the OSS/MLflow catalog.
- GPT off-thinking never emits `reasoning.effort: none`; Pi's `off`/`minimal`/`max` are suppressed for Grok rather than translated.
- Claude family aliases (`opus`/`sonnet`/`haiku`/`fable`) remain family-slotted.
- Codex CLI / smart routing / web search remain on `/ai-gateway/codex/v1`.
- OpenCode keeps main's DeepSeek support.
- The MLflow repair proxy restores direct configuration and shuts down cleanly.
- Main's smart routing v2, `ucode doctor`, Codex OS-scope settings, and Claude 2.1.248 requirement survive the sync unmodified.

## Execution steps

1. Back up: `backup/dev-before-pr-sync-<ts>` branch plus a protected stash ref for the dirty worktree (`uv.lock`, `.pi-subagents/`).
2. `git fetch origin`; merge `origin/main` into `dev`; resolve conflicts with the recorded precedents in `miniwiki/dev-consolidation.md` (shared Claude 1M policy over inline regex, `_discover_claude_gateway_ids` as the single legacy path routed through `_get_anthropic_models_json`, unioned `want_*` discovery sets in `cli.py`, keep both sides' tests).
3. Expect new conflict work against #434 (legacy routing removal), #416 (Codex OS-scope settings), and #415 (Claude OS-managed settings). Do not resurrect legacy routing that #434 deleted.
4. Cut `feat/pi-consolidated-improvements` from the synced `dev`.
5. Restore `uv.lock` to `origin/main`'s version; drop `verification/`, `miniwiki/`, `.gitignore`, and the two `scripts/prompt_cache_*` files plus their tests from the branch.
6. Re-order/reword commits into the ten-commit series above.
7. Run the validation gates below and record results here.
8. Human opens the PR with the body outline below. Agents do not push or open it.

## Validation gates (must all be recorded before requesting review)

- `uv run --frozen pytest -q -k 'not TestStateFileIsNotRewritten and not test_user_agent_arrives_at_gateway'`
- Focused: `uv run --frozen pytest -q tests/test_agent_pi.py tests/test_databricks.py tests/test_cli.py tests/test_mlflow_proxy.py tests/test_agent_opencode.py`
- `uv run --frozen pytest tests/test_e2e.py` with no `UCODE_TEST_WORKSPACE` (expect collection-only pass/skips)
- `uv run --frozen ruff check .` and `uv run --frozen ruff format --check src/ tests/`
- `uv run --frozen ty check src`
- `git diff --check origin/main...HEAD`, `git merge-base --is-ancestor origin/main HEAD`, conflict-marker scan
- Baseline adjudication: re-confirm on a detached `origin/main` worktree that `TestStateFileIsNotRewritten`, `test_user_agent_arrives_at_gateway`, and the two smart-routing socket tests fail/hang identically. Any exclusion without a reproduced baseline is a blocker.
- **New requirement for this PR:** at least one live-workspace run with `UCODE_TEST_WORKSPACE` set, covering `ucode pi` launch on (a) a Claude model, (b) a Grok model, and (c) an MLflow/OSS model, to close the open residual that the native Responses route fix was never confirmed against a live gateway.

## PR body outline

- Problem: Pi's model catalog, routing, and streaming diverge from what the gateway actually serves.
- What changes: capability-based discovery, native Responses route, MLflow OSS provider with SSE repair, managed allowlist enforcement, correct reasoning levels.
- Supersedes: #217, #223, #333, #334, #335 (and #336 partially, diagnostics deferred).
- Non-goals: prompt-cache diagnostics, OpenCode-specific fixes beyond shared discovery, Fable default inclusion, Codex CLI route changes.
- Verification: paste the recorded gate results, including the live-workspace run.
- Known residuals: real-state test hangs and the Claude user-agent e2e failure, both reproduced on unchanged `origin/main`; Grok rate-limit/429 capacity behavior tracked separately.

## Risks

- 32 commits of drift means the sync, not the code, is the main cost. Budget the merge separately from review.
- #434's legacy-routing removal is the highest-conflict area; a bad resolution silently reintroduces deleted routing.
- The PR is large (~7.5k lines including tests). If reviewers push back, the fallback is to split at commit 5: discovery/routing first, then the Pi MLflow provider and managed-allowlist work.
- Reviewers will likely ask why `uv.lock` is untouched; the answer is that `pyproject.toml` is unchanged.

## Residuals and next action

- Next action: create the backup refs and perform the `origin/main` → `dev` sync (step 1–2). Nothing else should start before that lands cleanly.
- No live Databricks e2e has been run for any revision of this work so far; `UCODE_TEST_WORKSPACE` was unset in every prior session.
