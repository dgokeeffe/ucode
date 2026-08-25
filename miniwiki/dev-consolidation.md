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

`origin/main` was at `95d2356` and was merged into `dev` as `cbde22e`. The ancestry check passed, and `dev` is now 15 commits ahead of `origin/main` with no commits behind it. Main's Claude native discovery, status/managed-config presentation, MCP agent targeting, Claude relay buffering fix, and reproduction harness were retained alongside the consolidated agent work.

Pi now performs a Pi-only supplemental Claude inventory lookup when writing an unmanaged config. It unions all Claude ids from the cached UC model-services walk with the legacy Anthropic gateway listing when UC is empty, non-Claude-only, or partial. The shared `claude_models` map remains pinned to Opus 4.8 for smart-routing compatibility and therefore remains Pi's default; Opus 5 is registered in Pi's Claude provider and can be selected explicitly. Managed `pi_models` remains authoritative and preserves multiple versions in one Claude family. Fable's existing opt-in behavior is unchanged.

Verification for this revision (auditable record: `verification/dev-opus5-c45740b.yaml`):

- `uv run --frozen pytest -q tests/test_agent_pi.py tests/test_databricks.py tests/test_cli.py`: **611 passed**.
- `uv run --frozen pytest -q -k 'not TestStateFileIsNotRewritten and not test_user_agent_arrives_at_gateway'`: **2093 passed, 37 skipped, 13 deselected in 76.51s**.
- `uv run --frozen pytest -q` was attempted but returned no result after hanging in the known real-state `TestStateFileIsNotRewritten` path; the bounded suite above completed.
- `uv run --frozen ruff check src tests`: passed.
- `uv run --frozen ruff format --check src/ tests/`: **81 files already formatted**.
- `git diff --check`: passed; `git merge-base --is-ancestor origin/main HEAD`: passed.
- No live Databricks e2e was run because `UCODE_TEST_WORKSPACE` was not set.

Independent review found and the parent reproduced/fixed: (1) same-family managed Claude ids being dropped, (2) no supplemental discovery for non-Opus Claude-only states, and (3) legacy gateway fallback being skipped for non-Claude or partial UC inventories. Reviewers also raised Fable inclusion, but that conflicts with the pre-existing explicit Fable opt-in invariant and was intentionally not changed.

## Residuals and next action

- The full suite's unchanged real-state tests remain unavailable because they hang; the completed bounded suite excludes that class and the known Claude user-agent test.
- No live Databricks e2e was run in this integration session because `UCODE_TEST_WORKSPACE` was not set.
- `uv.lock` and `.pi-subagents/` remain pre-existing local worktree changes and were not included in the implementation commit.
- The local `dev` branch is not pushed by this work. Pushing or opening/updating a PR remains an explicit human action.
