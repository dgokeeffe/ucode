# Gateway-only model discovery

## Problem report

"When we invoke ucode we are missing some of the models available in the Unity AI Gateway in
`ai-dev-tools` (e.g. grok-4-6, glm-5-4), in Pi."

## Investigation (workspace `https://dbc-a5d4177a-49dc.cloud.databricks.com`, profile `ai-dev-tools`)

Live inventories compared:

| Source | Count | Notes |
| --- | --- | --- |
| UC `system.ai` model-services (`list_model_services`) | 55 | the only inventory `discover_model_services` used |
| Foundation-model catalog (`/api/2.0/serving-endpoints:foundation-models`) | 58 | superset; also carries AI Gateway V2 api types |
| Serving endpoints (`/api/2.0/serving-endpoints`) | 59 | `databricks-*` names |

Catalog/endpoint entries with no UC registration: `gemini-3-7-flash`,
`gemini-3-1-flash-lite-image`, `kimi-k3-neo`.

Named examples from the report:

- `grok-4-6` **is** discovered and **is** in `~/.pi/agent/models.json` under `databricks-openai`;
  `system.ai.grok-4-6` and `databricks-grok-4-6` both answer 200 on
  `/ai-gateway/openai/v1/responses`. Not a discovery gap — a stale-state effect (see below).
- `glm-5-4` does not exist on this workspace (only `glm-5-2`, `glm-5-3`, `glm-5-3-flash`).

Root cause (confirmed): `discover_model_services` took its id inventory **only** from the UC
`system.ai` listing and used the foundation-model catalog just to *classify* those ids. The
per-family AI Gateway fallbacks (`discover_gemini_models`, `discover_codex_models`,
`discover_oss_models`) run only when a family bucket comes back empty, so on a workspace where UC
returns most models the gateway-only models were dropped silently.

Routability probes (2026-09):

- `system.ai.gemini-3-7-flash` → 404 `NOT_FOUND`; `databricks-gemini-3-7-flash` → 200.
- `databricks-kimi-k3` → 200; `system.ai.glm-5-3` → 200.
- `databricks-kimi-k3-neo` → 404 `ENDPOINT_NOT_FOUND` even though the catalog reports
  `state.ready = READY` and `ai_gateway_v2_supported = true`. Workspace-side inconsistency; ucode
  admits it because the catalog is the only signal available, matching the pre-existing
  serving-endpoint fallback behaviour.

## Change

`src/ucode/databricks.py`:

- New `_foundation_model_v2_endpoint_ids(payload)` — READY, `ai_gateway_v2_supported` endpoint ids
  from the cached foundation-model catalog. Missing `state` is treated as ready so a listing that
  omits the field cannot hide a working model.
- New `_gateway_only_model_ids(uc_ids, payload)` — catalog ids whose canonical name has no UC
  equivalent, minus embedding/rerank services. UC-registered models keep their `system.ai.*` id, so
  no `databricks-*` twin is ever added.
- `discover_model_services` now fetches the catalog first, unions the gateway-only ids into the
  inventory, and buckets everything through the existing capability-driven `supports_api` path.
- Claude family selection now sorts on the canonical name, so a mixed inventory still picks the
  newest version instead of the alphabetically-later id prefix (`system.ai.` > `databricks-`).

## Verification

- `uv run pytest tests/test_databricks.py -q` → 336 passed, including three new tests:
  gateway-only admission, unready/v1-only/non-chat exclusion, newest-Claude across mixed spellings.
- Full suite (`uv run pytest -q`, e2e files excluded): 2299 passed. Pre-existing failures on a clean
  tree (verified via `git stash`): `tests/test_claude_smart_routing_v2.py` (2 pty/socket tests) and
  `tests/test_e2e_user_agent.py::TestClaudeUserAgent::test_user_agent_arrives_at_gateway`.
- `uv run ruff check .` and `uv run ruff format` clean for the touched files.
- Live `discover_model_services` on `ai-dev-tools` now returns `databricks-gemini-3-7-flash`,
  `databricks-gemini-3-1-flash-lite-image`, `databricks-kimi-k3-neo` alongside the UC ids; Pi model
  entries render correctly for the `databricks-*` spelling (`_pi_gpt_model_entry`,
  `_pi_oss_model_entry` both normalize the prefix).

## Follow-up report: `clampThinkingBudgetToAnswerRoom` export error on glm-5-3

Symptom: selecting `glm-5-3` in a running Pi session →
`Error: The requested module './simple-options.js' does not provide an export named
'clampThinkingBudgetToAnswerRoom'`.

Not a ucode/gateway/model problem. Evidence:

- Process ancestry: the Pi session (PID 43606, launched by `ucode pi`) started **Thu 27 Aug
  10:33**. The global package was reinstalled **Tue 1 Sep** —
  `~/.npm/_logs` shows `npm install --global @earendil-works/pi-coding-agent@0.84.4` and
  `...@latest` runs that day (`install_tool_binary(update_existing=True)` from
  `ucode configure`/`setup`, plus a manual install).
- The installed 0.84.3 tree is self-consistent:
  `node_modules/@earendil-works/pi-ai/dist/api/simple-options.js` **does** export
  `clampThinkingBudgetToAnswerRoom`, and the bundled CLI inlines it in
  `dist/bundle/chunks/openai-completions-JD4WAC3R.js` (no `./simple-options.js` reference at all).
- Mechanism: Node caches ESM module records per URL for the process lifetime. The pre-upgrade
  (unbundled) Pi had loaded the old `simple-options.js`; `openai-completions.js` is only loaded
  the first time an `openai-completions` model is used. Selecting glm-5-3 pulled the *new*
  `openai-completions.js` from disk and linked it against the *cached old* sibling → missing export.
- Gateway side is healthy: `system.ai.glm-5-3` on `/ai-gateway/mlflow/v1/chat/completions` returns
  200 plain, with `reasoning_effort`, and with tools.

Fix: restart the Pi session. Possible ucode hardening (not implemented): refuse or warn before
`npm install -g <agent>` while a session of that agent is running.

## Residual / follow-up

- **Stale state**: discovery runs at `ucode configure` / first-time auto-configure only. Launches
  rebuild agent configs from `~/.ucode/state.json`, so newly shipped gateway models appear only after
  a re-run of `ucode configure`. A refresh TTL (or `--refresh-models`) on the launch path is not
  implemented and was left out of scope.
- `databricks-kimi-k3-neo` is offered but currently 404s at the gateway (workspace-side).
