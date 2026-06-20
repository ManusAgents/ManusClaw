# Changelog

All notable changes to **ManusClaw** are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

---

## [5.1.1] — 2026-06-20

### Summary

v5.1.1 is a **maintenance / bug-fix release** that closes 32 bugs discovered
in v5.1.0 across the agent core, security layer, FastAPI server, cron
scheduler, secrets redaction, multi-agent role pipeline, and observability
subsystem.

All fixes are covered by the existing test suite plus two new regression
tests; the full suite runs **212 passed, 2 skipped, 0 failed** (was 210
passed, 2 failed in v5.1.0). Static-analysis coverage of F821 (undefined
names) and F841 (unused-but-assigned locals) is now **0 errors** (was 23).

### Added

- **Regression tests** for the webhook router route-ordering bug
  (`tests/test_webhooks.py`):
  - `test_webhook_router_create_endpoint_not_swallowed_by_catchall` —
    POSTs to `/webhooks/create` via the real FastAPI `TestClient` and
    asserts `200` (was `404` before the route-order fix).
  - `test_webhook_router_trigger_still_works_after_reorder` — ensures
    the parameterised `POST /webhooks/{hook_id}` route still triggers
    webhooks with non-literal IDs after the reorder.
- **Changelog section in README.md** (`## 🔧 What's Fixed in v5.1.1`)
  with a tabular breakdown of every fix.
- **This file** (`CHANGELOG.md`) — first formal release audit trail.

### Changed

- Bumped version `5.1.0` → `5.1.1` in:
  - `pyproject.toml`
  - `app/cli.py` (`VERSION` constant and the welcome banner string)
  - `app/server/main.py` (FastAPI app `version`, `/healthz` payload,
    lifespan startup log, root endpoint message)
  - `run_server.py` (ASCII-art banner)
  - `build_release.py` (`TAG_NAME` template — was still on `v4.0.0`)
  - `README.md` header, footer, and badges.

### Fixed

#### Critical — runtime crashes / broken endpoints

- **`app/agent/router.py`** — `AgentRegistry._evict_idle` was declared
  `async` but called from sync `get()` / `put()` without `await`, so
  eviction never ran (idle-TTL test expected `None` but got the cached
  agent). Converted `_evict_idle` to a sync method; agent `cleanup()`
  coroutines are now scheduled fire-and-forget via `_safe_create_task`.
  Also fixed the LRU `put()` path that called `move_to_end` but did not
  overwrite the stored agent when re-inserting an existing key.
- **`app/cli.py`** — `logger` was referenced in two functions without
  being imported → `NameError` on the Spinner long-operation exit path
  and the background-task checkpoint-restore path. Fixed with local
  `from app.logger import logger as _logger` imports at the use-site.
- **`app/conversation/stuck_detector.py`** — `_action_fingerprint`
  referenced undefined `tool_call` instead of the local `tool_name` →
  `NameError` on every action without a `.function` attribute, breaking
  stuck detection.
- **`app/integrations/slack.py`** — `re` was used in
  `@self._bolt_app.action(re.compile(...))` but never imported →
  `NameError` on Slack Bolt action-handler registration. Added
  `import re`.
- **`app/server/webhook_router.py`** — **Route ordering bug**:
  `@router.post("/{hook_id}")` was declared before
  `@router.post("/create")`, so FastAPI matched the parameterised path
  first and `POST /webhooks/create` returned `404` ("Webhook 'create'
  not found"). Create / list / delete were all broken via HTTP. Reordered
  the router so literal sub-paths (`/create`, `/sign/{hook_id}`) come
  before the parameterised catch-all, and documented the ordering
  requirement in the module docstring.
- **`app/observability/health.py`** — `LLMHealthChecker._test_api_call`
  was `def` (sync) but called `llm.ask(...)` which is async → returned
  a coroutine object that was silently discarded (F841 `result`). The
  health check would always report success regardless of the LLM's
  actual state. Re-implemented to bridge the sync/async boundary via a
  worker thread running `asyncio.run`. Also fixed the call signature:
  `LLM.ask` takes a list of `Message` objects, not a string.
- **`app/llm/profile_rotation.py`** — `ModelProfile.default()`
  exception fallback called `cls(name="default")` but `__init__` does
  not accept `name` → `TypeError` masked the original error. Construct
  the profile first, then set `.name`.
- **`app/llm/credential_pool.py`** — Forward-reference `"ModelProfile"`
  triggered F821 (undefined name) under strict type checking. Use
  `TYPE_CHECKING` import so the symbol is resolvable for type checkers
  without creating a runtime circular import.
- **`app/observability/metrics.py`** — `Union` was used in three
  module-level type hints but never imported → F821 on module import
  under strict checkers. Added `Union` to the existing `typing` import.
- **`app/voice/talk.py`** — `Any` was used in two instance-variable
  annotations but never imported → F821. Added `Any` to the existing
  `typing` import.

#### Logic & correctness

- **`app/cron.py`** — `_JOBS_FILE = Path(os.getenv(...))` was evaluated
  ONCE at module import. Runtime changes to `MANUSCLAW_CRON_FILE`
  (tests, profile switching, CLI overrides) were silently ignored.
  Replaced with `_get_jobs_file()` lazy resolver called inside
  `_load_jobs` / `_save_jobs`.
- **`app/cron.py`** — `manusclaw-cron --trigger JOB` did not `return`
  after triggering → fell through to `asyncio.run(scheduler.run_forever())`
  and blocked the terminal forever. Added `return`.
- **`app/cron.py`** — `--list` output overwrote `output` on every loop
  iteration (`output = f"{t}"`) instead of appending, so only the LAST
  output_target was ever shown. Use `output += f" {t}"` and strip.
- **`app/skills/skill_engine.py`** — Same module-level-eval bug as
  cron.py: `_SKILLS_DIR` was set at import time and ignored subsequent
  `MANUSCLAW_SKILLS_DIR` changes. Replaced with `_get_skills_dir()`
  lazy resolver; updated `_load_user()` and `create()` to call it.
- **`app/tool/memory_tool.py`** — Same bug: `_WORKSPACE` /
  `MEMORY_FILE` / `USER_FILE` frozen at import. The `tmp_workspace`
  pytest fixture set `MANUSCLAW_WORKSPACE` at runtime, but
  `MemoryTool.execute()` still wrote to the import-time path — tests
  passed only because they manually monkey-patched `mt.MEMORY_FILE`.
  Added `_get_workspace()` / `_memory_file()` / `_user_file()` lazy
  resolvers; rewrote `execute()` to use them.
- **`app/task_queue.py`** — Same bug: `_WORKSPACE` / `_DB_PATH`
  evaluated at import. Added `_get_db_path()` lazy resolver;
  `TaskQueue.__init__` calls it when no explicit path is provided.
- **`app/llm/secret_redaction.py`** — AWS-secret pattern used a
  non-capturing prefix group `(?:secret_key...|aws_secret...)` so
  `redact()` replaced the entire match including the prefix —
  `secret_key=ABC...` became `***REDACTED***` (prefix lost). Converted
  to a capturing group and use the `\1` backreference pattern, matching
  the other redaction rules.
- **`app/integrations/resolver.py`** — `clear_results(older_than_hours=24)`
  computed `cutoff` but never used it — every terminal-status result was
  removed regardless of age, breaking the documented "older than N hours"
  contract. Now uses `started_at` to filter by age, with a safe default
  (keep results we can't prove are old enough).

#### Resource leaks

- **`app/agent/roles/engineer.py`** — `Manus()` instances were created
  for the main pass and the retry pass but `cleanup()` was never called
  → leaked Bash subprocesses (and any other tool resources) for the
  lifetime of the process. Wrapped each Manus run in `try/finally` with
  a `_cleanup_agent` helper.
- **`app/agent/roles/qa.py`** — Same leak as engineer.py: the QA Manus
  agent was never cleaned up. Added `try/finally` with cleanup call.

#### Test pollution

- **`tests/test_voice.py`** — `test_get_tts_provider_returns_nulltts_stub`
  did `tts_mod._create_provider = lambda name: ...` — a permanent
  module-level monkeypatch that leaked into every subsequent test in
  the file, causing `test_get_tts_provider_preferred_openai` to receive
  `NullTTS` instead of `OpenAITTS`. Use the `monkeypatch` fixture so
  the override is automatically restored at test teardown.

#### Dead-code / F841 cleanup

- **`app/canvas/tool.py`** — `_add_chart` captured
  `state = await self._server.update(...)` but never used it. Now
  reports the resulting component count for consistency with the other
  canvas method.
- **`app/file_store/s3.py`** — `write_stream` computed
  `key = self._make_key(path)` but never used it. Removed the
  assignment but kept the call for its path-traversal-validation
  side-effect.
- **`app/conversation/local_conversation.py`** — `_do_fork` computed
  `fork_log_path` but never used it. Now logged at DEBUG level so the
  path is visible in diagnostics.
- **`app/integrations/webhook_handler.py`** —
  `handler_result = await handler(event)` discarded result. Replaced
  with bare `await handler(event)` + explanatory comment.
- **`app/parallel_executor/executor.py`** —
  `call_lookup = {c.call_id: c for c in calls}` built but never used.
  Removed with explanatory comment (results are correlated via `zip()`).
- **`app/llm/litellm_client.py`** — `except Exception as e: ... raise`
  — `e` unused. Dropped the `as e` binding.
- **`app/observability/health.py`** — Two
  `except Exception as e: ... raise` blocks — `e` unused. Dropped the
  `as e` bindings.
- **`app/voice/wake.py`** — `sample_width = 2` assigned but never used.
  Converted to a comment so the int16 / 2-byte intent is preserved for
  future readers porting to other audio libraries.
- **`app/integrations/resolver.py`** — `service` and `content` bindings
  unused in the `apply_changes` path. Removed bindings with explanatory
  comments noting why the calls are still made (validation side-effect).

### Security

No new CVE-class vulnerabilities were introduced or fixed in this
release. The existing security controls were audited as part of the
bug-fix pass:

- **Path-traversal protection** in `LocalFileStore._resolve()` was tested
  against `../../../etc/passwd`, `/etc/passwd`, `a/../../b`, `../outside`,
  `subdir/../../../etc/passwd` — all blocked with
  `FileStorePermissionError`.
- **Command-injection surface** in `Bash` / `DockerSandbox` /
  `OpenShellSandbox` was audited — all use `asyncio.create_subprocess_exec`
  (no shell), so no shell-metacharacter injection is possible at the
  transport layer. Catastrophic-command blocking remains at the regex
  layer in `app/permissions/gate.py` and `app/tool/bash.py`.
- **Secret redaction** regex set was hardened (see logic fix above) so
  context prefixes (`secret_key=`, `aws_secret=`) are preserved when
  redacting the secret value.

### Verification

- `pytest` → **212 passed, 2 skipped, 0 failed** (was 210 passed,
  2 failed in v5.1.0).
- `ruff check app/ --select F821,F841` → **0 errors** (was 23).
- FastAPI `TestClient` HTTP smoke test against `/healthz`, `/`,
  `/tools`, `/sessions`, `/webhooks` (create / list / trigger-with-HMAC /
  delete) — all pass.
- Module import audit — all 133 main modules import cleanly under
  Python 3.12.
- Path-traversal audit — see Security section above.

---

## [5.1.0] — 2026 (prior release)

Enterprise-grade enhancement release. See the `## 🆕 What's New in v5.1`
section in [README.md](README.md) for the feature highlights, and the
`911fc1e` / `6a0b7f7` commits on `main` for the implementation history.

---

## Maintenance Policy

- **Patch releases** (`5.1.x`) — bug fixes, regression tests, and
  security hardening only. No new features, no breaking API changes.
- **Minor releases** (`5.x.0`) — new features, optional dependency
  additions, and backward-compatible API extensions.
- **Major releases** (`6.0.0`) — breaking API changes; will be
  accompanied by a migration guide.

To report a bug or request a backport, open an issue at
<https://github.com/ManusAgents/ManusClaw/issues> and tag it with
`bug` + the affected version.
