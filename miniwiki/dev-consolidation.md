# Consolidated development branch

## Objective

Keep a local `dev` integration branch that contains current `origin/main` plus the behavior from these pull requests:

- #243 — Pi configuration isolation with `PI_CODING_AGENT_DIR`
- #217 — validated GLM/Kimi MLflow provider for Pi
- #333 — shared Claude/GPT context capability policy
- #223 — GPT off-reasoning compatibility
- #239 — OpenCode GPT routing and OSS fallback discovery
- #334 — active OSS/MLflow capability discovery and latest-GPT selection
- #335 — MLflow SSE repair and proxy safety
- #336 — Pi session affinity and cache diagnostics

This page records the local integration only. It does not authorize pushing, merging, or changing the individual PR branches.

## 2026-08-20 integration

`origin/main` was fetched at `0a7b554` (`Add DeepSeek models to OpenCode (#359)`). PR #243 was already merged into that base as `00e3233`. The remaining feature commits were replayed from the current fork PR heads, omitting the duplicate #243 patch, and the independent #336 cache-affinity commits were applied after #335.

Two mainline/stack conflicts were resolved deliberately:

- Preserve main's newly validated DeepSeek family while retaining the conservative OSS capability metadata and dynamic MLflow discovery from #217/#334.
- Preserve main's `PI_CODING_AGENT_DIR` e2e isolation semantics rather than restoring the older comment/assumption from the stacked OpenCode change.

The integration review found that the older managed-Pi family splitter predated the new MLflow provider. It omitted OSS models from `pi_models`, which could reintroduce workspace-wide unlisted models. The integration adds OSS to the managed allowlist, clears stale Pi defaults for an unservable nonempty allowlist, and starts the MLflow repair proxy for managed OSS-only configurations. Focused regression tests cover all three cases.

## Impact map

Production paths:

- Agent configuration and launch: `src/ucode/agents/{claude,opencode,pi}.py`, `src/ucode/agents/_mlflow_proxy.py`, `src/ucode/agents/__init__.py`
- Model discovery and capability policy: `src/ucode/databricks.py`
- Managed-model classification: `src/ucode/managed_resolve.py`
- CLI orchestration: `src/ucode/cli.py`
- Cache diagnostics: `scripts/prompt_cache_http_trace.py`, `scripts/prompt_cache_repro.py`

Compatibility invariants:

- `origin/main` remains an ancestor of the consolidated branch.
- Pi uses `PI_CODING_AGENT_DIR` and does not replace `HOME`.
- A nonempty managed Pi model allowlist cannot fall back to unlisted discovery results.
- OpenCode retains main's DeepSeek support.
- GPT off-thinking does not emit unsupported `reasoning.effort: none`.
- Active capability data controls dynamic OSS models and latest-GPT selection.
- The MLflow repair proxy restores direct configuration and shuts down safely.

## Verification evidence

Deterministic validation on the integrated tree:

- `uv run --frozen pytest` cannot complete because three `TestStateFileIsNotRewritten` tests hang on unchanged `origin/main`; a representative test also timed out after 45 seconds in a detached `origin/main` worktree.
- `uv run --frozen pytest -q` with those three baseline hangs and the independently reproduced Claude user-agent baseline failure deselected: **2058 passed, 37 skipped, 4 deselected in 128.05s**.
- Focused affected suite: **975 passed, 3 deselected in 49.04s**. The three deselections are the same baseline-hanging managed-state tests.
- `tests/test_e2e.py` without `UCODE_TEST_WORKSPACE`: **1 passed, 30 skipped**.
- `uv run --frozen ruff check .`: passed.
- `uv run --frozen ruff format --check src/ tests/`: **80 files already formatted**.
- `git diff --check origin/main...HEAD`: passed.
- Ancestry check (`git merge-base --is-ancestor origin/main HEAD`): passed.
- Conflict-marker scan: passed.

Baseline adjudication:

- `tests/test_e2e_user_agent.py::TestClaudeUserAgent::test_user_agent_arrives_at_gateway` fails identically on detached `origin/main`: the installed Claude binary returns a response without contacting the capture server.
- `tests/test_managed_resolve.py::TestStateFileIsNotRewritten::test_developers_state_file_keeps_their_own_model` times out identically on detached `origin/main`. Two adjacent tests using the same real-state/Claude writer path also hang in the integrated full run.

Independent review:

- Initial squad workflow `495fd947-4f27-498d-9134-74e9d87535f6` found the managed Pi OSS allowlist omission; the parent reproduced it and added write-path coverage.
- Targeted re-review `236e13b1-7f95-4cbe-b13e-acd8253c536f` confirmed that omission was fixed and identified stale settings/proxy lifecycle follow-ups.
- Final targeted re-review `edd19e8a-e749-4b82-adde-6a6a676d4a61` reported no evidenced blockers after those follow-ups.

## Recovery and rollback

The pre-sync branch tip and complete dirty worktree were preserved before integration:

- Branch: `backup/dev-before-main-sync-20260820-102249`
- Protected stash ref: `refs/ucode-backups/dev-pre-main-sync-20260820-102249`
- Stash object: `ea968e16ed02b16385e65133bf6d41bedff42183`

The stash includes the old untracked diagnostic files, local `tests/test_e2e.py` edit, unrelated `uv.lock` rewrite, and prior orchestration artifacts. Nothing from it was discarded. Rollback is to repoint `dev` to the backup branch; individual files can be recovered from the protected stash ref.

## 2026-08-25 main sync and Pi Claude inventory

`origin/main` was at `95d2356` and was merged into `dev` as `cbde22e`. The ancestry check passed, and `dev` is now 19 commits ahead of `origin/main` with no commits behind it. Main's Claude native discovery, status/managed-config presentation, MCP agent targeting, Claude relay buffering fix, and reproduction harness were retained alongside the consolidated agent work.

Pi now performs a Pi-only supplemental Claude inventory lookup when writing an unmanaged config. It unions all Claude ids from the cached UC model-services walk with the legacy Anthropic gateway listing when UC is empty, non-Claude-only, or partial. The shared `claude_models` map remains pinned to Opus 4.8 for smart-routing compatibility and therefore remains Pi's default; Opus 5 is registered in Pi's Claude provider and can be selected explicitly. Managed `pi_models` remains authoritative and preserves multiple versions in one Claude family. Fable's existing opt-in behavior is unchanged. UC pagination now rejects malformed tokens, avoids caching incomplete walks, and supplements a partial Claude family map before it reaches the CLI.

Verification for this revision (auditable record: `verification/dev-opus5-c45740b.yaml`):

- `uv run --frozen pytest -q tests/test_agent_pi.py tests/test_databricks.py tests/test_cli.py`: **620 passed**.
- `uv run --frozen pytest -q -k 'not TestStateFileIsNotRewritten and not test_user_agent_arrives_at_gateway'`: **2102 passed, 37 skipped, 13 deselected in 77.93s**.
- `uv run --frozen pytest -q` was attempted but returned no result after hanging in the known real-state `TestStateFileIsNotRewritten` path; the bounded suite above completed.
- `uv run --frozen ruff check src tests`: passed.
- `uv run --frozen ruff format --check src/ tests/`: **81 files already formatted**.
- `git diff --check`: passed; `git merge-base --is-ancestor origin/main HEAD`: passed.
- No live Databricks e2e was run because `UCODE_TEST_WORKSPACE` was not set.

Independent review found and the parent reproduced/fixed: (1) same-family managed Claude ids being dropped, (2) no supplemental discovery for non-Opus Claude-only states, and (3) legacy gateway fallback being skipped for non-Claude or partial UC inventories. Reviewers also raised Fable inclusion, but that conflicts with the pre-existing explicit Fable opt-in invariant and was intentionally not changed.

## 2026-08-27 main sync and Grok visibility in Pi

`origin/main` advanced to `81d03f1` and was merged into `dev` as `261a911`. The merge retained main's managed-config export, Codex routing, and Claude gateway-discovery proxy changes. Main renamed the shared proxy header constant to the public `HOP_BY_HOP_HEADERS`; the inherited Pi MLflow repair proxy was reconciled to that name for both request and response filtering.

The reason `system.ai.grok-4-6` was not auto-detected was the UC-first classifier: it admitted only model names containing `gpt-` to the Responses catalog. When UC returned any GPT model, the catalog was nonempty, so the capability-based legacy fallback never ran and Grok was silently omitted. Discovery now recognizes both GPT and Grok as OpenAI Responses families while continuing to exclude `gpt-oss`. Grok therefore propagates through shared state into Pi's `databricks-openai` provider and model picker; it is explicitly excluded from the OSS/MLflow catalog. Managed Pi model classification follows the same family rule.

Verification for this revision (auditable record: `verification/grok-pi-main-sync-261a911.yaml`):

- Focused discovery, CLI propagation, Pi, OpenCode, and managed-setup suites: **789 passed in 36.43s**.
- Proxy compatibility suites: **42 passed in 10.84s**.
- Bounded full suite: **2162 passed, 37 skipped, 13 deselected in 65.84s**.
- `uv run --frozen ruff check .`: passed.
- `uv run --frozen ruff format --check src/ tests/`: **89 files already formatted**.
- `git diff --check`, origin/main ancestry, and conflict-marker scans: passed.
- Requirements, regression, edge-case, and bounded security/correctness review left no reproduced blockers. Adjudicator run `4a4b9ceb` returned `READY_FOR_HUMAN_REVIEW`.

The existing e2e exclusions for Grok in Codex, Pi, and Copilot remain deliberate: this change makes Grok visible and correctly routed in Pi's catalog but does not claim those clients' current request shapes successfully invoke it.

### Capability-gated future models and context windows

The follow-up removes the need to add every future vendor name manually without blindly admitting all `system.ai.*` services. UC model-service IDs are now joined to the cached foundation-model catalog and bucketed only when a live V2 API is compatible: Responses models go to the OpenAI/Codex catalog, native Gemini models to Gemini, and MLflow-chat-only models to OSS. Embeddings and rerankers remain excluded, native routes cannot be duplicated into OSS, and known-family name fallbacks apply only when metadata for that exact model is unavailable. Claude remains family-slotted because its clients require `opus`/`sonnet`/`haiku`/`fable` aliases.

Grok 4.6 had also inherited the generic 128K Responses fallback. Its static metadata now reflects Databricks' documented **500K context window**. For Grok and future Responses models, context windows parsed from the live foundation-model description are persisted as `codex_model_specs` and used by both Pi and OpenCode; existing conservative output ceilings and reasoning compatibility remain unchanged. Common `K`, `M`, `million`, and `500,000-token` description forms are supported. MLflow models likewise choose the largest context advertised by their MLflow-capable entities.

Verification for this follow-up (auditable record: `verification/capability-context-discovery-e4aa6aa.yaml`):

- Focused discovery, CLI, Pi, OpenCode, managed-setup, and proxy suites: **845 passed in 44.76s**.
- Bounded full suite: **2176 passed, 37 skipped, 13 deselected in 64.92s**.
- No-workspace e2e collection: **1 passed, 30 skipped**.
- Full Ruff, format, diff, ancestry, and conflict-marker checks passed.
- Review found and the parent fixed explicit-metadata/static-OSS precedence and duplicate endpoint output. Final edge, regression, and bounded security reviews reported no blockers. Adjudicator run `7e1ac5fe` returned `READY_FOR_HUMAN_REVIEW`.

### Native Responses route correction

Pi and OpenCode had conflated a model's `openai/v1/responses` capability with Codex CLI's coding-agent route, configuring `/ai-gateway/codex/v1`. They now use the documented native model-service base `/ai-gateway/openai/v1`, whose SDK adapters append `/responses`. Codex CLI, Codex smart routing, and web search remain on `/ai-gateway/codex/v1`; Claude, Gemini, MLflow, host construction, and authentication are unchanged. This correction is the likely resolution for the historical Pi/Grok 400, but it still requires a live workspace request to confirm.

Verification (auditable record: `verification/native-responses-route-97a25bb.yaml`): focused route/state/user-agent suites completed with **517 passed, 5 deselected**; the bounded full suite completed with **2176 passed, 37 skipped, 13 deselected**; Ruff, format, and diff checks passed. Requirements, edge, regression, and adjudicator run `0005b469` reported no blockers.

### Grok thinking controls in Pi

Pi previously enabled reasoning only for `gpt-5` IDs, so Grok appeared as a non-reasoning model despite being reasoning-only. Exact Grok 4.6 IDs now declare reasoning and expose precisely the Databricks-supported effort levels: `low`, `medium`, `high`, and `xhigh`. Pi's `off`, `minimal`, and `max` choices are suppressed rather than translated into unsupported gateway values; preview/future Grok IDs do not inherit these assumptions without validation. The installed Pi runtime's `getSupportedThinkingLevels` returned exactly those four levels for the generated entry.

Verification (auditable record: `verification/grok-pi-thinking-3feb9cd.yaml`): **641 focused tests passed**; the bounded full suite completed with **2178 passed, 37 skipped, 13 deselected**; `ty`, Ruff, format, and diff checks passed. Requirements and edge re-review reported no blockers, and adjudicator run `20085341` returned `READY_FOR_HUMAN_REVIEW`. A repeated live 429 reported for Grok is tracked separately as a rate-limit/capacity diagnostic requiring response headers; it is not a thinking-metadata failure.

## 2026-08-31 main sync

`origin/main` advanced to `2727132` (`smart routing: route Codex v2 subagents (#409)`) and was merged into `dev` as `1614fe3`. `dev` is now 29 commits ahead of `origin/main` and 0 behind; the ancestry check passes. The pre-sync tip was preserved as `backup/dev-before-main-sync-20260831-*`.

Main contributed Claude/Codex smart-routing v2 (PTY + first-prompt routing), `ucode publish`, role-aware managed-config paths, launch-time speedups, agent-name-preserving banners, Anthropic MPS family pinning, and bounded retries for Claude model discovery (#412).

Four conflicts were resolved deliberately:

- `src/ucode/agents/claude.py`: kept the shared `claude_model_supports_1m` policy for the `[1m]` suffix and dropped main's newly added inline `_CLAUDE_MODEL_RE`. The shared policy already normalizes both the `databricks-claude-*` gateway form and the `system.ai.claude-*` UC form, and additionally covers Sonnet 4.5 and Fable 5, which main's inline regex did not.
- `src/ucode/databricks.py`: kept `_discover_claude_gateway_ids` as the single legacy AI Gateway inventory path (it is also used by `discover_claude_models_unbucketed` and the UC-partial fallbacks) and routed it through main's `_get_anthropic_models_json`, so the consolidated branch inherits #412's bounded 429/network retries.
- `src/ucode/cli.py`: unioned the discovery `want_*` sets. `want_codex` keeps OpenCode alongside main's codex/copilot/pi, and `want_oss` keeps OpenCode/Pi alongside main's Codex smart-routing requirement. `_DISCOVERY_CONSUMERS["oss"]` now names `codex` too so the discovery diagnostic matches the actual fetch set.
- `tests/test_cli.py`: kept both sides' tests, including main's `test_codex_only_configure_persists_discovered_oss_models`.

Two dev tests that stubbed `_http_get_json` with a fixed `timeout=10` signature were widened to `**kwargs` for the new `max_retries` argument.

Verification for this sync:

- `uv run --frozen pytest -q -k 'not TestStateFileIsNotRewritten and not test_user_agent_arrives_at_gateway'`: **2306 passed, 37 skipped, 13 deselected in 114.62s**, with only the two baseline smart-routing socket failures below.
- `tests/test_claude_smart_routing_v2.py::TestFirstPromptHook::test_blocks_once_then_allows_replay` and `::TestPtyFlow::test_direct_switch_restore_and_replay` fail identically on a detached `origin/main` worktree (`2 failed, 20 passed`): the local prompt-routing unix socket never becomes ready in this environment. Environmental baseline, not a merge regression.
- `uv run --frozen ruff check .`: passed. `uv run --frozen ruff format --check src/ tests/`: **93 files already formatted**. `uv run --frozen ty check src`: passed.
- `tests/test_e2e.py` without `UCODE_TEST_WORKSPACE`: **1 passed, 30 skipped**.
- `git merge-base --is-ancestor origin/main dev`: passed. Conflict-marker scan of `src/` and `tests/`: clean.

## Residuals and next action

- The full suite's unchanged real-state tests remain unavailable because they hang; the completed bounded suite excludes that class and the known Claude user-agent test.
- No live Databricks e2e was run in this integration session because `UCODE_TEST_WORKSPACE` was not set.
- `uv.lock` and `.pi-subagents/` remain pre-existing local worktree changes and were not included in the implementation commit.
- The local `dev` branch is not pushed by this work. Pushing or opening/updating a PR remains an explicit human action.
- The two Claude smart-routing v2 socket tests remain unavailable in this environment; they fail the same way on unchanged `origin/main`.
- No live workspace check of main's new smart-routing v2 PTY path was run against the consolidated Pi/OSS work.
