# A/B Comparison: Core Lightning v26.04.1 vs Production (LNbits + DR Focus)

Date: 2026-05-20

## Scope

This assessment compares:

- A (current production): Core Lightning app `25.09.3-hotfix.1`, `cln-application:25.07.3`, `lightningd:v25.09.3`
- B (candidate): Core Lightning app `26.04.1`, `cln-application:26.04`, `lightningd:v26.04.1`

Primary decision focus:

- impact to LNbits in this Umbrel stack
- impact to DR rebuild and restart-survival procedures

## Evidence Sources

Local production contracts:

- `core-lightning/umbrel-app.yml`
- `core-lightning/docker-compose.yml`
- `core-lightning/exports.sh`
- `lnbits/docker-compose.yml`
- `janx-web/docs/DR-RESTART-TEST.md`

Upstream candidate contracts (getumbrel/umbrel-apps master):

- `core-lightning/umbrel-app.yml` (release notes explicitly: CLN `v26.04.1`, app `v26.04`)
- `core-lightning/docker-compose.yml`
- `core-lightning/exports.sh`
- `core-lightning/hooks/pre-start`

## Raw A/B Delta

### 1) Version and image deltas

- `umbrel-app.yml`
  - A: app version `25.09.3-hotfix.1`
  - B: app version `26.04.1`
- `docker-compose.yml`
  - A: `ghcr.io/elementsproject/cln-application:25.07.3`
  - B: `ghcr.io/elementsproject/cln-application:26.04`
  - A: `elementsproject/lightningd:v25.09.3`
  - B: `elementsproject/lightningd:v26.04.1`

### 2) Interface/contract deltas relevant to integrators

- Ports remain the same in A and B:
  - WS `2106`
  - REST `2107`
  - gRPC `2110`
- Exported contract in `exports.sh` is unchanged for key variables:
  - `APP_CORE_LIGHTNING_DAEMON_IP`
  - `APP_CORE_LIGHTNING_WEBSOCKET_PORT`
  - `CORE_LIGHTNING_REST_PORT`
  - hidden-service hostname export
- `lightningd` startup contract is functionally equivalent for integration points:
  - `--database-upgrade=true` still present
  - `--clnrest-host` and `--clnrest-port` still present
  - same bind pattern for P2P + WS + gRPC + REST

### 3) New/changed startup behavior

- Candidate includes a `hooks/pre-start` gate that waits for Tor hidden service file generation before full app startup.
- This is operationally positive for race reduction, but can expose startup timing issues if Tor init is degraded.

## LNbits Impact Assessment

### Current observed coupling

- Local `lnbits/docker-compose.yml` is configured for `LndRestWallet` (LND-backed), not CLN-backed.
- Therefore, stock local LNbits in this workspace is not directly coupled to CLN API semantics.

### janx-specific reality

- Existing janx runbooks and architecture docs show an LNbits-over-CLN deployment pattern elsewhere in the stack.
- For that path, critical dependency is CLN REST/WebSocket availability and auth material continuity, not a changed port contract.

### Risk rating: **Low to Medium**

- Low for protocol/port breakage: no contract changes found in exports/ports.
- Medium for runtime behavior:
  - CLN DB migration path (`--database-upgrade=true`) is one-way on first B boot.
  - Potential startup race side effects across Tor/CLN/LNbits chain remain the main risk class.

### Specific LNbits gates (must pass)

1. LNbits health endpoint returns OK after CLN upgrade and reboot.
2. Invoice creation succeeds through the active backend wallet path.
3. LNURL callback generation remains `https://` where Cloudflare full-proxy is used.
4. Any CLN-backed extension flow (nostrnip5/LNURL path) still mints and settles invoices.

## DR Rebuild Impact Assessment

### What changes materially for DR

- CLN database schema upgrade occurs on first B start (`--database-upgrade=true`).
- Existing `backupIgnore` still excludes `data/lightningd/bitcoin/lightningd.sqlite3` in both A and B.
- This means file-level backup/replay does not provide a simple DB rollback path.

### DR risk rating: **Medium to High**

- Medium for restart survivability (if services start cleanly, runbook still valid).
- High for rollback complexity after first B boot because of schema-forward migration and ignored sqlite in app backup contract.

### DR gates (must pass)

1. Full janx restart-survival test passes with zero manual remediation:
   - follow `janx-web/docs/DR-RESTART-TEST.md` sign-off criteria.
2. After reboot, CLN REST/WS/gRPC are reachable on expected ports.
3. LNbits payment flow and identity webhooks recover automatically.
4. No new degraded/down systems in `/api/status` post-upgrade.

## Decision

Recommendation: **Proceed with Conditions**

Rationale:

- A/B diff does not show interface-breaking changes in ports/exports/integration contract.
- Main risk is not API breakage; it is operational migration and rollback irreversibility after DB upgrade.

## Mandatory Preflight Checklist

1. Confirm current CLN data snapshot strategy (full app-data backup and off-host copy) before first B boot.
2. Record baseline health:
   - CLN getinfo, LNbits health, janx `/api/status`, LNURL/NIP-05 checks.
3. Confirm NPM template patch state if running Cloudflare full-proxy mode.
4. Schedule upgrade window with explicit rollback decision checkpoint before first customer traffic window.

## Rollback Criteria

Rollback is required if any of the following occur and are not resolved within the window:

1. CLN fails healthy startup or cannot serve REST/WS/gRPC on expected endpoints.
2. LNbits cannot create or settle invoices through the active backend path.
3. DR restart test fails any required check.
4. Critical janx identity/payment routes degrade (`/.well-known/lnurlp`, `/.well-known/nostr.json`, callback paths).

Note: after DB schema migration, rollback may require full app-data restore rather than image-only downgrade.

## Post-Upgrade Verification Pack

Run immediately after upgrade, then again after one controlled reboot:

1. CLN core checks: node info, channel list, wallet/connectivity checks.
2. LNbits checks: health endpoint, invoice creation, payment settlement.
3. janx checks: `janx.com`, `byob.janx.com`, `nostr.janx.com`, `/api/status`.
4. Identity/payment webhook checks:
   - `/.well-known/lnurlp/<user>`
   - `/.well-known/nostr.json?name=<user>`
   - LNURL callback returns invoice payload.

## Bottom Line

- Contract risk: low.
- Operational migration and DR rollback risk: medium-high.
- Go decision is acceptable only with strict preflight backups plus full restart-survival validation.
