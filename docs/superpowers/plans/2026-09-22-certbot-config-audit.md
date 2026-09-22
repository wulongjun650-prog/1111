# Certbot configuration audit implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this tightly coupled task. Steps use checkbox syntax for tracking.

**Goal:** Enforce a read-only, fail-closed private configuration audit at the existing command boundary.

**Architecture:** Keep fixed argv and disabled-by-default execution. Reuse bounded exclusive file reads and private-directory checks; accept only a small explicitly validated subset of Certbot's ConfigObj format. No new parser dependency, no rewriting foreign or unsupported configurations.

**Tech Stack:** Python standard library, existing certificate filesystem helpers, pytest.

**Spec:** `docs/superpowers/specs/2026-09-21-isolated-certificates-design.md`

## Global constraints

- Separate production/staging roots; exact `ab-<32位站点ID>` lineage and one domain.
- Private management directories 0700; configuration must be owner-only, non-linked regular files.
- Reject hooks, installers, unknown options, ambiguous syntax, foreign accounts/server/paths.
- Never register an account, accept terms, install a client, contact a CA, or enable the worker in this task.
- Root-controlled ancestors and the existing worker lock remain caller prerequisites. This is not protection against a concurrently malicious root administrator.
- This task covers configuration only. Certificate material validation, account provisioning, installed-client compatibility, persisted issuance intents and worker integration remain separate gates.

## Task 1: Audit and enforce configuration before process launch

**Files:** Create `outputs/ab-lab/ablab/certbot_config.py`, `outputs/ab-lab/tests/test_certbot_config.py`; modify `ablab/certbot_command.py`, its tests, the shared reader in `ablab/certificates.py` (empty CLI only, default remains nonempty), and `deploy/PROVISIONING-PROGRESS.md` under the same app directory.

**Interface:** `audit_private_config(root, identity, account_id, server, *, operation)` returns None or raises redacted `ProvisioningError`. Caller supplies the fixed environment root, validates identity/account/server using the command boundary, holds the worker lock, and rechecks authorization immediately before launch.

- [x] Write filesystem tests. Baseline fixture: 0700 root/config/work/logs/config/renewal; private `cli.ini` containing only a comment; renewal root values version/archive_dir/cert/privkey/chain/fullchain, `[renewalparams]` with authenticator=webroot, account, server, key_type, webroot_path and `[[webroot_map]]` with exact domain/path.
  ```python
  with pytest.raises(ProvisioningError):
      audit_private_config(root, identity, account, server, operation='renew')
  assert renewal.read_bytes() == before  # foreign options never repaired
  ```
- [x] Run `python -m pytest outputs/ab-lab/tests/test_certbot_config.py -q`; observe missing audit failing before implementation.
- [x] Implement narrow grammar: blank/comment lines; unique root key/value pairs; one renewalparams section, its webroot_map subsection, optional acme_renewal_info with whole-second ISO naive retry timestamp (fractional/timezone values deliberately rejected). Reject quoting, interpolation, inline comments, extra sections, duplicate keys and unknown options. Exact path/account/server checks; accepted optional config_dir/work_dir/logs_dir must equal private paths. RSA/ECDSA metadata is finite and validated. CLI accepts empty content or comments/whitespace only; renewal files must remain nonempty. First issue requires no target renewal/live/archive entry; renew and dry-run require the target renewal file. Never enumerate or alter unrelated lineages.
  ```python
  check_default_configs(root)
  audit_private_config(root, identity, self.account_id, server, operation=operation)
  if authorize() is not True:
      raise ProvisioningError('站点归属或暂停复核未通过，未执行证书操作')
  ```
- [x] Add command-boundary tests showing a failed audit prevents subprocess launch; existing launch-contract tests may stub only this separately tested filesystem gate. Add a real-audit integration case with temporary filesystem and fake subprocess.
- [x] Run focused suites, obtain independent review, address findings, run all Python and JS tests and `git diff --check`.
- [x] Record verification and remaining deployment gates; commit only scoped source/tests/docs.

## Verification 2026-09-23

- Initial missing-module RED: 55 failures, 2 platform skips. Control-character/pending cases: 9 RED then GREEN; umask contract: 3 RED then GREEN; naive timestamp: 1 RED then GREEN; empty CLI: 1 RED then GREEN.
- Independent review: empty CLI finding addressed; key-metadata cases and POSIX tests added; scoped re-review has no remaining findings.
- Final full suite: 436 Python passed, 33 platform skips, 2 existing deprecation warnings; 8 JavaScript passed; pip check and diff whitespace check passed.
- POSIX ownership/mode and symlink tests skipped on this Windows host are NOT Linux acceptance. Installed-client compatibility, material acceptance and worker integration remain outside this task.
- Read-only server check: no Certbot found; Ubuntu apt candidate 4.0.0-4. Installation simulated only; actual installation approval requested separately. Official v4.0.0 storage source additionally inspected at https://raw.githubusercontent.com/certbot/certbot/v4.0.0/certbot/certbot/_internal/storage.py.

## Source references / compatibility gate

Official sources inspected on 2026-09-22:
- https://eff-certbot.readthedocs.io/en/stable/using.html (5.8.0 documentation)
- https://raw.githubusercontent.com/certbot/certbot/master/certbot/src/certbot/_internal/storage.py (`make_renewal_configobj`, `relevant_values`)
- https://raw.githubusercontent.com/certbot/certbot/master/certbot/src/certbot/_internal/renewal.py (`CONFIG_ITEMS`, `_restore_webroot_config`, `acme_renewal_info`)

ConfigObj accepts more syntax than this audit intentionally permits. The actual installed client and generated configuration must pass an isolated compatibility test before live activation. Unknown version fields never justify expanding the allowlist without inspection.
