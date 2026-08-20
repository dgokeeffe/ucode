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

## Residuals and next action

- No live Databricks e2e was run in this integration session because `UCODE_TEST_WORKSPACE` was not set.
- The local `dev` branch is not pushed by this work. Pushing or opening/updating a PR remains an explicit human action.
