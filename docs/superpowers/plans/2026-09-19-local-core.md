# Local core implementation

Current user instructions supersede the September 2 spec where noted here.
Implement locally, no third-party settings changes, no public deployment, no login/billing UI.
One managed site; Python FastAPI + SQLite + vanilla Chinese UI, no frontend build chain.
Source deliverable: outputs/ab-lab. Work/ holds environment and intermediate evidence.

## Defined behavior
- PAGE and LINK content modes; RULES/FORCE_A/FORCE_B routing modes.
- RULES with protection OFF selects B. Force modes precede protection.
- Active protection priority: blacklist > whitelist (bypass remaining rules) > generic UA bot marker > IPv4 > PC > minimum Android/iOS version > supplied blocked CIDRs > country > language > visit limit.
- First N document GET requests pass visit limit, N+1 fails. Per normalized IP, fixed window starting with first document visit, default 24h. All document visits increment, including blocked/force visits. HEAD/assets/previews/simulation do not increment. Explicit reset has audit record.
- Unknown country only blocks when countries selected. GeoIP optional local MMDB. Device/UA guesses are spoofable; no verified bot identity, no invented risk database.
- Single immutable version per import; publish/rollback atomic pointer. Missing selected content returns 503 maintenance, never another slot.
- ZIP limits 20MiB compressed,100MiB expanded,20MiB/member,500 files. index.html root or one wrapper. HTML direct upload supported. Server scripts/configs/symlinks/traversal rejected.
- Admin 127.0.0.1:8765; target 127.0.0.1:8766. Both loopback only. Admin exact Host, socket peer, Origin and nonce checks, no CORS; all uploaded active content only on target, iframe sandbox without same-origin. No public unauthenticated deployment.
- Separate preview capability endpoints generated via admin; signed short-lived path; no decision/log/counter side effects.
- Each target request evaluates rules; non-document assets use current count (no increment). Limit compares asset current count>N, document next count>N. Existing HTML may mix assets during config switch; documented boundary.
- No arbitrary external fetching. Links admin-configured HTTP(S) only, no credentials. Random/round-robin/equal distribution; atomic counters. Logs scrub query/body, masked IP display. CSV formula neutralization.

## Tasks
1. Backend tests first: state/rule/counter/ZIP/HTTP isolation contracts; run RED. Implement models, store, rules, ZIP importer, admin/target apps. GREEN tests.
2. Independent UI sidecar: create templates/index.html and static/app.js,app.css against work/ui-contract.md. Dashboard, mode controls, A/B upload/version/preview, links, rules, simulator, logs; no fake controls. Main owns backend only; review and integrate UI.
3. Integration: safe runner, pinned requirements, README, demo pages; real HTTP test and browser interaction. Fix observed failures with regression tests.
4. Final independent review and verification. Package source (no data/env/private research) and show local UI. Report remaining limitations honestly.

## Verification commands
work/venv/Scripts/python.exe -m pytest outputs/ab-lab/tests -q
node --check outputs/ab-lab/static/app.js
work/venv/Scripts/python.exe outputs/ab-lab/run.py

## Progress
- Planning: inspected reference read-only; undocumented precedence/counting is our explicit policy, not claimed vendor internals.
- Ruling: use existing dedicated project on codex/ab-core, no new worktree or main changes; user asked to execute without another approval loop.
- Backend: initial RED (missing modules), implemented then 57 Python tests GREEN. Windows ZIP test writer was normalizing hostile backslashes; corrected fixture to preserve raw names.
- UI sidecar saved files but service authentication failed before reporting. Main reviewed/integrated saved UI, corrected integer OS fields and unsupported 90-day log option; 5 node tests GREEN.
- Browser: loaded console, uploaded/published demo A and B, iframe JS executed, FORCE_A and FORCE_B verified at same target URL, mobile-only rule blocked desktop and simulator allowed phone. Logs verified, no console errors. Restored default rules.
- Browser integration fix: native confirm dialog blocked CDP interaction; replaced with keyboard-accessible HTML dialog and verified publish with Confirm button.
- Local services running loopback ports8765/8766 with data in work/local-demo-data. No public deployment, no reference-site mutations.
- Current limitations fully documented in outputs/ab-lab/README.md; independent review requested after integration.
- Independent review was saved before reviewer quota failure. Found directory-index counting and relative-resource defects. Main reproduced RED, added canonical directory redirect (preserves preview capability) before counting. Final redirect destination counts exactly once. No independent re-review available; final verification by main.
