# Disaster Recovery: umbrelOS Restart Survival Test

Cross-references: [ARCHITECTURE](./ARCHITECTURE.md) · [PROBES](./PROBES.md) · [README](../README.md)

Goal: prove the entire janx.com surface (public + internal + sibling apps) self-heals after a hard reboot of the umbrelOS host with no manual steps.

## What persists by design

| Layer                                                   | Mechanism                             | Path / location                                                      | Survives reboot?                              |
| ------------------------------------------------------- | ------------------------------------- | -------------------------------------------------------------------- | --------------------------------------------- |
| App definition                                          | umbrel-apps clone on host             | `/home/umbrel/umbrel/umbrel-apps/janx-web/`                          | yes                                           |
| Container code                                          | bind mount `./data:/app:ro`           | `/home/umbrel/umbrel/umbrel-apps/janx-web/data/`                     | yes                                           |
| Probe definitions                                       | file in mount                         | `data/probes.json`                                                   | yes                                           |
| App enabled state                                       | umbreld                               | umbreld DB                                                           | yes                                           |
| NPM proxy hosts                                         | NPM SQLite + generated nginx confs    | `/data/nginx/proxy_host/{6,22,23,24}.conf` (in NPM container volume) | yes (volume)                                  |
| NPM `_location.conf` template patch (CF full-proxy fix) | in-container file edit                | `/app/templates/_location.conf` inside `nginx-proxy-manager_web_1`   | **no** — must re-apply after NPM image update |
| Cloudflare tunnel                                       | cloudflared service                   | `/etc/systemd/system/cloudflared.service` (or app)                   | yes                                           |
| Cloudflare cache state                                  | external                              | dash.cloudflare.com                                                  | yes (manual purge if stale)                   |
| Operator notes (LSP tab textarea)                       | browser localStorage `janx-lsp-notes` | client-side only                                                     | survives in browser, **not** on server        |

## What does NOT persist

- Anything written to a container layer that is not in a bind mount or named volume
- The NPM template patch (see above) — re-apply after `docker compose pull` of NPM
- Any `localStorage` / `sessionStorage` data on the browser side once that browser is cleared

## Pre-test checklist

```bash
ssh umbrel@janx
# 1. confirm app on disk
ls /home/umbrel/umbrel/umbrel-apps/janx-web/{docker-compose.yml,umbrel-app.yml,data/server.py,data/probes.json}
# 2. confirm container running
docker ps --filter name=janx-web_ --format 'table {{.Names}}\t{{.Status}}'
# 3. confirm probes green
curl -sS http://127.0.0.1:8099/api/status | jq '.systems[] | {id,status}'
# 4. snapshot NPM confs
sudo md5sum /home/umbrel/umbrel/app-data/nginx-proxy-manager/data/nginx/proxy_host/*.conf
```

## Restart procedure

```bash
ssh umbrel@janx
sudo reboot
```

Wait ~90 seconds. From a separate shell:

```bash
# Wait for SSH back
until ssh -o ConnectTimeout=2 umbrel@janx 'echo up' 2>/dev/null; do sleep 5; done
```

## Post-restart verification

Run all of the following. Every one must pass:

```bash
# 1. App stack reattached
ssh umbrel@janx 'docker ps --filter name=janx-web_ --format "{{.Names}}\t{{.Status}}"'
#   Expect: janx-web_web_1     Up
#           janx-web_home_dev_1 Up

# 2. Public site
curl -sS -o /dev/null -w '%{http_code}\n' https://janx.com/
#   Expect: 200

# 3. Public portal-config (verify production defaults)
curl -sS https://janx.com/api/portal-config | jq
#   Expect: {"home":{...,"target":"prod"},"public":{"lspTabEnabled":false},...}

# 4. Citadel mirror
curl -sS -o /dev/null -w '%{http_code}\n' https://janx.com/citadel/
#   Expect: 200

# 5. Internal portal (LSP tab visible)
curl -sS -H 'Host: janx.local' http://janx.local:40080/ | grep -c 'data-tab="lsp"'
#   Expect: >= 1

# 6. Sibling apps
curl -sS -o /dev/null -w '%{http_code}\n' https://nostr.janx.com/
curl -sS -o /dev/null -w '%{http_code}\n' https://byob.janx.com/
#   Expect: 200 (or 401 for byob if auth-walled)

# 7. All probes green
curl -sS http://janx.local:40080/api/status \
  | jq -r '.systems[] | "\(.status)\t\(.id)"' | sort
#   Expect: all "ok" (or documented-degraded)

# 8. Nostr WebSocket NIP-01 still up
curl -sS http://janx.local:40080/api/status \
  | jq -r '.systems[] | select(.id=="nostr-relay-ws") | .status'
#   Expect: ok
```

## Failure remediation

| Symptom                                                  | Likely cause                                                                                                             | Fix                                                                                                                                                           |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `janx-web_web_1` not Up                                  | umbreld didn't restart it                                                                                                | `cd ~/umbrel && sudo ./scripts/app start janx-web`                                                                                                            |
| 502 from janx.com                                        | NPM upstream DNS race vs janx-web                                                                                        | restart NPM: `docker restart nginx-proxy-manager_web_1`                                                                                                       |
| BYOB tab loads but LNbits 502 over CF                    | `_location.conf` template patch lost                                                                                     | re-apply NPM template patch (see `/memories/repo/cloudflare-lnbits-proxy-runbook.md`)                                                                         |
| LNbits CLN app restart loop (`Exit 1`) after CLN restore | stale CLNRest rune cache in `lnbits-cln` (`.clnrest-runes`) that no longer matches current CLN node identity or rune set | remove stale cache then restart app: `rm /home/umbrel/umbrel/app-data/lnbits-cln/data/.clnrest-runes && cd ~/umbrel && sudo ./scripts/app restart lnbits-cln` |
| nostr-relay-ws degraded                                  | Cloudflare WebSocket setting toggled off                                                                                 | enable WebSockets in CF dashboard for nostr.janx.com zone                                                                                                     |
| Recent Errors yellow                                     | jemalloc page-size crash                                                                                                 | confirm image is `>= sha256:3ab815568452...` (16KB-page build)                                                                                                |
| Stale page on janx.com only                              | Cloudflare cache                                                                                                         | manual purge in dashboard for janx.com zone                                                                                                                   |

## LNbits-CLN DR Validation Drill (Rune Cache Failure)

Use this only as a controlled test to verify the recovery path. This intentionally breaks LNbits-CLN until cache is restored.

```bash
# 1) Backup current rune cache
ssh umbrel@janx 'cp /home/umbrel/umbrel/app-data/lnbits-cln/data/.clnrest-runes /home/umbrel/umbrel/app-data/lnbits-cln/data/.clnrest-runes.bak'

# 2) Simulate stale/invalid runes
ssh umbrel@janx 'sed -i "s/^APP_LNBITS_CLN_READONLY_RUNE=.*/APP_LNBITS_CLN_READONLY_RUNE=\"FAKE_STALE_RUNE\"/" /home/umbrel/umbrel/app-data/lnbits-cln/data/.clnrest-runes'

# 3) Restart LNbits-CLN and observe startup loop/failure
ssh umbrel@janx 'cd ~/umbrel && sudo ./scripts/app restart lnbits-cln'
ssh umbrel@janx 'docker logs lnbits-cln_web_1 --tail 120'

# 4) Recover by removing stale cache (forces pre-start to re-provision runes)
ssh umbrel@janx 'rm /home/umbrel/umbrel/app-data/lnbits-cln/data/.clnrest-runes && cd ~/umbrel && sudo ./scripts/app restart lnbits-cln'

# 5) Verify LNbits-CLN health
ssh umbrel@janx 'docker ps --filter name=lnbits-cln_web_1 --format "{{.Names}}\t{{.Status}}"'

# 6) Optional cleanup: restore original cache backup if needed
ssh umbrel@janx 'test -f /home/umbrel/umbrel/app-data/lnbits-cln/data/.clnrest-runes.bak && cp /home/umbrel/umbrel/app-data/lnbits-cln/data/.clnrest-runes.bak /home/umbrel/umbrel/app-data/lnbits-cln/data/.clnrest-runes'
```

## Sign-off criteria

A restart test is **passed** when:

- All 8 verification commands return expected output
- No manual remediation steps were taken between `sudo reboot` and verification
- `/api/status` shows zero `down` and zero `degraded` for the Nostr group
