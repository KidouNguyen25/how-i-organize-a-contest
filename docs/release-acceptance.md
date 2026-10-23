# Open issues — release acceptance and evidence register

Updated: 2026-10-23. Source reviewed from the repository. **No live deployment, load test or two-season integrated run was performed as part of this audit.** Existing integration and unit tests are not production acceptance evidence.

## #5 — B02/B07: compiler isolation and recovery

State: **OPEN — implementation required**.

Current behavior:
- `ott/api.py` starts `_coding_watcher` inside the API process and invokes `_process_submission`, `build_final`, and `compile_source`. The `code/check` API route compiles synchronously.
- `--no-dispatch` separates match dispatch only; `python -m ott worker` does not process compiler jobs.
- SQLite stores immutable final-submission receipts (`received_at`, source, source hash, status); resource slots use leases/fencing.
- Bot runtime state cannot be restarted mid-match; the current mode intentionally sends such runs to review.

Acceptance gate:
- [ ] Implement a standalone compiler job consumer and replace synchronous `code/check` with asynchronous receipt/status or a bounded isolated mechanism. Separate API and compiler processes and resource privileges.
- [ ] Compiler worker crash/hang cannot prevent API `GET /readyz`, `GET /api/v1/tournaments/{id}/code` and receipt creation.
- [ ] Demonstrate termination, requeue, worker restart, lease expiry/fencing, exactly-once finalization or safe idempotent reprocessing, and strict quota refund/charge.
- [ ] Prevent old workers from overwriting artifacts after losing their lease and document retry exhaustion. Count and audit every infrastructure failure.
- [ ] Document that bot RAM resume is unsupported; explicitly test `PAUSED_REVIEW` rather than claiming mid-match resumption.
- [ ] CI tests run on Linux Docker and attached logs identify commit and image digest.

## #6 — A06/A08: Core and OTT integration

State: **OPEN — implementation required**.

Current behavior:
- `CoreAPIv2Controller` keeps definitions, seasons, entries and results in in-memory dictionaries; this is distinct from persisted `ott.store.Store`.
- `TestSuiteAdapter` and `BotAdapter` have contracts and isolated tests; the live OTT controller/API does not route season dispatch through this Core controller.
- `tests/test_phat_deliverables.py::test_a08_dual_seasons_on_unified_core` is a useful unit exercise, not evidence that CP and OTT ran via one persistent controller, scheduler, DB and Docker setup.

Acceptance gate:
- [ ] Define adapter-to-`judge/` execution and test-case verdict mapping, including C++17 compile errors, timeout, deterministic scoring and source hash.
- [ ] Connect OTT adapter to the existing queue/store/service instead of duplicating a second independent in-memory tournament registry.
- [ ] Provide one persistent season/competition repository and one controller dispatching CP and OTT; keep mode-specific logic in adapters.
- [ ] Run two seasons sequentially without changing Core code, restart the process and verify both results survive; exercise organizer/contestant scopes and no cross-season data leak.
- [ ] Save commands, fixture hashes, Docker image IDs, returned results and CI URLs bound to the tested commit.

## #7 — C05/C06/C08: Cloudflare BFF and public portal

State: **OPEN — source contract reconciled, real staging evidence required**.

Verified from source: backend validates origin secrets (including previous key), exact public-origin mutation, bounded 1 MiB body and secure cookie; Worker has header allowlist, no automatic mutation retry and unit tests. `docs/minh/bff-contract.md` now describes the implemented security contract instead of a superseded 403 issue.

Acceptance gate:
- [ ] Deploy tested `deploy/cloudflare/src/worker.js` via explicit `wrangler.toml`; provide worker URL, commit, deployment version and sanitized configuration hash.
- [ ] Run `smoke.mjs` with `PORTAL_URL`, `ORIGIN_URL`, `ORIGIN_KEY` and test-account variables. Capture **all** W01–W08, O01/O02, A01–A03; a skipped scenario is not a pass.
- [ ] Browser manual test: login, mutation with valid/invalid CSRF, host-only Secure/HttpOnly/SameSite cookie, logout.
- [ ] Observe polling of foreground/background tabs, Retry-After and a tunnel outage/recovery, with request logs and screenshots (no credentials).
- [ ] Report the real 30-client, 4-hour or agreed representative load test, including Workers request counts, origin saturation, failure and recovery.

## #8 — B04–B08: Linux staging, resilience and release

State: **OPEN — infrastructure deployment and evidence required**.

Acceptance gate:
- [ ] Clean dedicated Linux VM; record distro/kernel, Docker/server and runtime image digest, git commit, ports and network boundary. Never commit live credentials or secret values.
- [ ] Verify source allowlist, origin authentication, readiness, database/schema/space and role-scoped operational API. Record `/healthz`, `/readyz`, `/api/v1/ops/status` outcome through the intended trusted interfaces.
- [ ] Review and *actually apply* `deploy/firewall.nft` with console/rollback available, approved DNS/NTP/tunnel endpoint sets, and before/after ingress/egress tests; a syntax check alone does not qualify.
- [ ] With all API/worker/compiler processes stopped, `python -m ott backup --data data --destination ../backup-<run> --offline`, `verify-backup`, `restore --destination <new empty path>` and `migrate-copy` against another new path; verify row counts, hashes, permissions and encrypted off-host retention.
- [ ] Create `release-manifest` at tested commit, confirm `working_tree_dirty=false`, report `config_sha256`, image IDs, source allowlist hash, migration rehearsal, and rollback drill.
- [ ] Run end-to-end competition and load tests for 30 teams/clients with observed error and latency distributions. Save logs, exit codes and a reviewer signature.

## Evidence handling and sign-off

Required record for each acceptance scenario:
- `issue`, `scenario_id`, `run_at_utc`, `git_commit`, `environment`, `config_sha256`, `image_digest`, `command`, `result`, `artifact_sha256`, `reviewer`.
- Retain signed/verified evidence in a restricted location and link the redacted copy from the issue. Do not commit secret values, session cookies, password dumps or tunnel origin authentication keys.
- Record `NOT_RUN` and `BLOCKED` separately from `PASS`; no release-complete label while any mandatory gate remains unverified.

## Dependency order

`#5 compiler isolation` → `#6 shared Core integration` → `#7 gateway staging` → `#8 full production-like release`. BFF source validation is useful independently but does not waive worker/runtime or security gates.
