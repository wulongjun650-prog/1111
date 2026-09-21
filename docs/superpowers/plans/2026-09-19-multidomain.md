# Multi-domain implementation

Spec: ../specs/2026-09-19-multidomain-auto-provision-design.md (approved by user).

Constraints: preserve legacy store/auth and existing packages; no real panel mutations; only registered domains; private data outside public roots; no guessed panel contracts or false SSL success. Work in existing feature branch because application is untracked; no git identity changes.

## Task 1: Registry and routing
- [x] Add failing tests for domain normalization, duplicate/reserved rejection, default migration idempotency, private independent stores, unknown-host rejection, cross-site version/signature rejection.
- [x] Implement `ablab/sites.py`: SQLite registry separate from business Store, immutable ID paths, default legacy mapping, persisted provisioning status.
- [x] Extend `web.py` with catalog and explicit `/api/sites/{id}/...` routes, central authentication and per-request store selection; retain default legacy APIs.
- [x] Run registry, HTTP, production and existing tests.

## Task 2: DNS and provisioning
- [ ] Verify official panel contracts before selecting adapter; document unresolved capabilities honestly.
- [x] Add DNS all-record checks, bounded timeouts, explicit unconfigured state and persisted retry/pause; test with controlled external transports.
- [ ] Add worker, restricted provisioning adapter and server configuration only where contracts are verified; unsupported capability must fail closed.
- [ ] Test conflict, restart, timeout reconciliation, secret redaction and certificate failure. No production success claim without real integration.

### 2026-09-20 continuation: recoverable job orchestration

User approved continuing code and local tests before deleting dmvfvj.vip. Existing remote sites remain untouched. Browser inspection confirmed aaPanel 8.0.4 / Nginx 1.30.2, but no credentialed API contract test has run; current browser inventory is empty. Continue locally while user restores the session. Keep the HTTP adapter disabled until its complete contract is validated.

- [x] Add `tests/test_provisioning_jobs.py` first. Exercise the real SQLite registry and coordinator with an external-panel test double: success, conflict, preexisting foreign directory, missing DNS, create timeout/restart reconciliation, ambiguous disappearance, pause during an external call, TLS/renewal verification failure, and two workers.
- [x] Implement `ablab/provisioning_jobs.py` with a non-blocking process lock in a worker-private directory, durable creation intent, fixed per-ID paths, verified ownership before writes, and explicit phases. External adapter interface: `inspect(domain, path) -> dict | None`, `create(identity)`, `configure(identity, panel_id)`, `certificate(identity, panel_id)`, `verify(identity, panel_id) -> {https: bool, route: bool, renewal: bool}`. `inspect` must reject foreign directories and enumerate aliases; this is an adapter obligation, not evidence that the target panel supports it.
- [x] Keep checkpoints in worker-private SQLite separate from web-writable registry. Record creation intent before calling the panel; if restart finds no owned object after intent, require manual reconciliation instead of blindly creating again. Never delete remote objects, never log exception text, and mark active only after explicit HTTPS/routing/renewal evidence.
- [x] Run focused then full regressions, document which evidence is simulated and which remains remote-only. Do not ship this as a finished auto-provision release.

Runnable checks: `work/verify-venv/Scripts/python.exe -m pytest outputs/ab-lab/tests/test_provisioning_jobs.py -q`, then the full `outputs/ab-lab/tests` suite. The module has no live panel default and is deliberately not wired into `run.py --service dns` until an authenticated adapter and service isolation are available.

### 2026-09-20 live read-only API verification

This supersedes the earlier missing-session / untested-authentication notes only; write operations are still unimplemented and unverified.

- With explicit user approval, enabled the panel API and persisted a custom source whitelist containing only `127.0.0.1`. No key reset, site mutation or other access expansion.
- Used the already authenticated panel Linux terminal to perform server-local HTTP requests. No credentials copied to this workspace, browser clipboard, chat or public HTTP requests.
- Actual configuration is `/www/server/panel/config/api.json`, not `data/api.json`. The configuration contains an internal `token` plus `token_crypt`. Do not interpret the internal token as the UI plaintext key.
- Initial diagnostic double-hashed the internal token and failed with `Secret key verification failed`. Read-only inspection of the installed `/www/server/panel/class/common.py` confirmed its signature comparison uses `md5(request_time + api_config['token'])`. Corrected only the diagnostic signing expression; no panel changes or decryption were required. Existing plaintext-key preflight semantics should not be changed indiscriminately.
- Verified `POST /v2/site?action=GetPHPVersion`: HTTP 200, integer status 0, returned list includes static version `00`.
- Verified `POST /v2/data?action=getData` with `table=sites`, exact test-name filtering after search: integer status 0; `dmvfvj.vip` is ID 22, path `/www/wwwroot/dmvfvj.vip`, status `1`.
- Rechecked whitelist in the same server-local diagnostic: exactly `['127.0.0.1']`.
- Existing `/opt/ab-lab`, `/etc/ab-lab`, `/var/lib/ab-lab` directories are present. System Python reports 3.14.4; panel interpreter symlink points to Python 3.12. Do not assume the application runtime matches either without checking its service.
- This verifies read-only authenticated connectivity, NOT automatic creation, proxy configuration, certificates, renewal, rollback or recovery. All three existing test sites remain intact. No new release was built in this verification step.

### 2026-09-20 coordinator and local preflight implementation

- Added coordinator and 28 checks including an independent Python process contending on the OS lock, KeyboardInterrupt recovery, web-catalog identity tampering and orderly pause immediately after durable intent. Remote panel/DNS operations remain test doubles; no concrete write adapter or live worker wiring.
- Added local-config preflight and `deploy/inspect_panel.py --local-config --address ...`: internal digest is not double-hashed, plaintext-key flow remains supported, signed fields move from URL to POST body. Exact loopback and source whitelist checks fail closed. No credentials are persisted or printed. This new command has local tests but has not yet been deployed to the server.
- Baseline before changes: 115 Python tests. Final full regression after changes: 154 Python tests, 8 JavaScript tests pass; two existing dependency deprecation warnings remain. Job tests: 28, preflight/DNS: 24, CLI: 2.
- Added `outputs/ab-lab/deploy/PROVISIONING-PROGRESS.md` with commands and remaining gates. Preserved release ZIPs and server state. Independent read-only review found an orderly-pause intent issue; reproduced both branches with failing tests, fixed untouched-intent rollback, and obtained follow-up limited-scope acceptance with 28 job tests rerun. No live provisioning or production-ready claim.

### 2026-09-21 adapter development checkpoint (not a release)

- Implemented `PanelSites`, `ManagedPanel`, and `NginxEntry` plus 50 focused tests. Real domain binding read-only API verified earlier: integer status 0, 50 bindings; ID22 apex and www. Static AddSite request is narrowly scoped, default disabled, and not live-tested. Concrete adapter remains disconnected from the DNS service; certificate/verify methods explicitly fail closed.
- Earlier remote HTTP entry syntax test passed with exact generated-config SHA-256 `47976f33751c9766d6dddc49bf9e917723d74aca084b06463e84b694c96b2565`, using Nginx `-t` on an anonymous memfd. No live config save or reload; not routing/TLS/global-merge validation.
- Independent review defects reproduced and repaired: preserve the exact displaced config instead of lossy replace, recheck ownership before rollback, reject parent traversal and overlap before mkdir, canonicalize Unicode aliases, and permit bounded JSON expansion in receipts. Pending markers block uncertain retries; retain private adjacent snapshots. No automatic conflict rollback or snapshot cleanup.
- Full final local result: **204 Python / 8 JavaScript tests pass**, two existing dependency deprecation warnings. Follow-up reviewer reran **50 scoped tests**, accepted fixes; no new confirmed findings. Native Windows filesystem operations exercised; Linux exchange/durability/permission behavior still requires real runtime validation.
- Browser terminal unavailable (blank, red connection indicator); refresh returned to panel entrance and control timed out. No server files, existing sites, certificates or service state modified in this checkpoint. No new ZIP shipped. Continue after terminal restoration; no need for user to send passwords in chat.
- Remaining overall Task 2 gates: subdirectory/manual-config conflict coverage, certificate issuance/renewal effect scope and user-approved ACME terms if needed, worker/service permission isolation, concrete wiring, controlled live creation and HTTPS/routing/renewal acceptance. Existing test-site deletion requires backup and explicit action-time confirmation.

### 2026-09-21 restored terminal checkpoint

- User restored terminal. Six isolated file-operation probes passed on Linux, first on `/tmp` and then on the same filesystem as live Nginx configuration. Exact source helper hash: `a96abc1d8f2028cc52843c4c12aa88e81681259936636f9a7de610b07447267c`. Retained newly created private test fixtures; no site mutation/reload, no crash/power-loss experiment, no full worker evidence.
- Read-only service check: admin and target active as `ab-lab`, DNS worker not installed. App venv Python exists. No runtime upgrades or service changes.
- Verified binding-table API response envelope live (status integer 0, empty list). Added root/subdirectory combined conflict checks with five failing-then-passing regressions, including matching wildcard, invalid PID, added binding on an owned site, and unrelated-binding allowance.
- Full regression now 209 Python + 8 JavaScript passed; two pre-existing warnings. Independent follow-up review accepted the narrow binding change and reran 28 focused tests. Existing release ZIPs unchanged. Certificate/renewal, manual config conflicts, worker isolation/wiring and live end-to-end creation remain outstanding.

## Task 3: Admin interface

### Certificate decision checkpoint (2026-09-21)

Read-only AST hashing of installed aaPanel confirms ACME save/sync/register/account-query functions match cached reference. Cross-site certificate copying and web reload are real installed behavior, not merely an inferred upstream risk. Installed deployment/renew-toggle/task-status API functions also match; apply_new_ssl differs and is not validated. No credential/private-key export or certificate API mutations. Ask user whether to retain tightly preflighted panel-shared certificate integration or approve independent managed-site certificate lifecycle before implementing a different architecture. All prior write/activation gates remain closed.

- [x] Site selector, domain list/add, DNS instructions and pause/retry with real stages.
- [x] Bind requests and stale results to selected site; prevent changes during writes and resolve unsaved drafts.
- [x] Test helpers and browser switching, uploads and per-domain config persistence.

## Task 4: Deployment and verification
- [x] Update setup, upgrade/backup and worker configuration docs with remaining integration gates.
- [x] Run full Python/JS tests and browser evidence; inline review performed, independent review unavailable.
- [x] Build separate v2-preview package excluding data, credentials, caches; verify extracted package.

## Progress / decisions
- Baseline: 75 Python tests passed. Current: 105 Python / 6 JS tests passed before final packaging.
- Keep current checkout: untracked application would be lost in a clean worktree; no destructive checkout or move.
- Central auth stays in legacy Store; separate catalog contains no panel secret.
- Sidecar API researcher failed with account usage limit, no files produced. Independent review cannot run under same limit; inline review and automated tests used, not claimed independent review.
- Official API verifies Static/00 and AddSite path/SSL flags, but reverse proxy, validation/reload and certificate renewal contracts for target version remain unresolved. Read-only PanelPreflight implemented; no guessed write adapter or fake ACTIVE state. Full Task 2 remains blocked pending version-specific sanitized API evidence / separately authorized test-server integration.
- Browser: registered demo-one.example.com / demo-two.example.com locally, switched allowed A on first while second retains B, uploaded/published B.html only on second; signed iframe preview loaded. Original default data preserved.
- Found local target_url missing final slash during browser review; regression first failed with default maintenance response, now fixed and suite passes.
- Deliver development preview only, not production-ready auto-provision release. Preserve old release packages.
- Final source run: 105 Python tests, 6 Node tests, JS syntax passed. ZIP contains 43 files, 89947 bytes, SHA256 9d01618142ddaee5199fb99569269df6235d6892d06f80484d3c079b2a543358. Extracted to work/v2-preview-verify-d907021ac17f4c08807792ae9e8cadd3 for separate test run.
- Browser confirmed final slash after restart, dirty-rule switch dialog appears, cancelled then confirmed switch leaves target site's blacklist empty. Local browser left on domain management; service session 45601. Original sites and external panel untouched.
