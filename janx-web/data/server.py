import json
import os
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

from flask import Flask, jsonify, send_from_directory

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PROBES_PATH = os.path.join(APP_DIR, "probes.json")
RUN_COMMAND_PROBES = os.environ.get("RUN_COMMAND_PROBES", "false").lower() == "true"
PUBLIC_LSP_TAB_ENABLED = os.environ.get("PUBLIC_LSP_TAB_ENABLED", "true").lower() in (
    "1",
    "true",
    "yes",
    "on",
)
HOME_VIEW_TARGET = os.environ.get("HOME_VIEW_TARGET", "prod").strip().lower()
HOME_VIEW_DEV_URL = os.environ.get("HOME_VIEW_DEV_URL", "/home-dev.html")
HOME_VIEW_PROD_URL = os.environ.get("HOME_VIEW_PROD_URL", "/home-dev.html")
NIP11_SOURCE_URLS = [
    url.strip()
    for url in os.environ.get(
        "NIP11_SOURCE_URLS",
        "http://janx.local:4848/,https://nostr.janx.com/",
    ).split(",")
    if url.strip()
]
NIP11_TIMEOUT_SECONDS = int(os.environ.get("NIP11_TIMEOUT_SECONDS", "5"))

app = Flask(__name__)


def resolve_home_source_url():
    target = HOME_VIEW_TARGET if HOME_VIEW_TARGET in ("dev", "prod") else "dev"
    return HOME_VIEW_DEV_URL if target == "dev" else HOME_VIEW_PROD_URL


@app.after_request
def disable_cache(resp):
    # Ensure browsers/CDNs never reuse stale probe snapshots.
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_probe_config():
    with open(PROBES_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


def fetch_nip11(url):
    req = urllib.request.Request(
        url=url,
        headers={
            "Accept": "application/nostr+json",
            "User-Agent": "janx-web-monitor/1.0 (+https://janx.com)",
        },
    )

    with urllib.request.urlopen(req, timeout=NIP11_TIMEOUT_SECONDS) as resp:
        code = getattr(resp, "status", 200)
        payload = json.loads(resp.read().decode("utf-8", errors="replace"))
        if not isinstance(payload, dict):
            raise ValueError("NIP-11 payload is not a JSON object")
        return code, payload


def run_http_probe(system):
    url = system.get("url")
    timeout = int(system.get("timeoutSeconds", 10))
    headers = {
        "User-Agent": "janx-web-monitor/1.0 (+https://janx.com)",
        "Accept": "*/*",
    }
    headers.update(system.get("headers", {}))

    req = urllib.request.Request(url=url, headers=headers)
    start = time.time()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            elapsed = int((time.time() - start) * 1000)
            code = getattr(resp, "status", 200)
            if 200 <= code < 400:
                status = "ok"
            else:
                status = "degraded"
            return {
                "status": status,
                "summary": f"HTTP {code}",
                "latencyMs": elapsed,
                "evidence": [f"{url} -> {code}"],
            }
    except urllib.error.HTTPError as err:
        elapsed = int((time.time() - start) * 1000)
        return {
            "status": "degraded",
            "summary": f"HTTP {err.code}",
            "latencyMs": elapsed,
            "evidence": [str(err)],
        }
    except Exception as err:
        elapsed = int((time.time() - start) * 1000)
        return {
            "status": "down",
            "summary": "HTTP probe failed",
            "latencyMs": elapsed,
            "evidence": [str(err)],
        }


def run_tcp_probe(system):
    host = system.get("host")
    port = int(system.get("port", 0))
    timeout = int(system.get("timeoutSeconds", 5))
    start = time.time()

    try:
        with socket.create_connection((host, port), timeout=timeout):
            elapsed = int((time.time() - start) * 1000)
            return {
                "status": "ok",
                "summary": f"TCP {host}:{port} reachable",
                "latencyMs": elapsed,
                "evidence": [f"Connected to {host}:{port}"],
            }
    except Exception as err:
        elapsed = int((time.time() - start) * 1000)
        return {
            "status": "down",
            "summary": f"TCP {host}:{port} unreachable",
            "latencyMs": elapsed,
            "evidence": [str(err)],
        }


def run_docker_health_probe(system):
    """Query Docker healthcheck status for a container"""
    container_name = system.get("container")
    timeout = int(system.get("timeoutSeconds", 5))
    start = time.time()

    try:
        proc = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .State.Health}}",
                container_name,
            ],
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        elapsed = int((time.time() - start) * 1000)

        if proc.returncode != 0:
            return {
                "status": "unknown",
                "summary": "Container not found",
                "latencyMs": elapsed,
                "evidence": [f"docker inspect {container_name} failed"],
            }

        try:
            health = json.loads(proc.stdout)
            if not health or health.get("Status") is None:
                return {
                    "status": "unknown",
                    "summary": "No health check configured",
                    "latencyMs": elapsed,
                    "evidence": [
                        "Add 'healthcheck:' block to docker-compose.yml"
                    ],
                }

            status_map = {"healthy": "ok", "unhealthy": "degraded", "starting": "unknown"}
            docker_status = health.get("Status", "unknown")
            mapped_status = status_map.get(docker_status, "unknown")

            # Extract last few log lines as evidence
            log_lines = health.get("Log", [])
            evidence = [
                (
                    entry.get("Output")
                    or f"[{entry.get('ExitCode')}]"
                )
                for entry in log_lines[-2:]
            ]
            if not evidence:
                evidence = [docker_status]

            return {
                "status": mapped_status,
                "summary": f"Health: {docker_status}",
                "latencyMs": elapsed,
                "evidence": evidence,
            }
        except json.JSONDecodeError:
            return {
                "status": "unknown",
                "summary": "Could not parse health status",
                "latencyMs": elapsed,
                "evidence": [proc.stdout[:200]],
            }
    except subprocess.TimeoutExpired:
        elapsed = int((time.time() - start) * 1000)
        return {
            "status": "down",
            "summary": "Health probe timeout",
            "latencyMs": elapsed,
            "evidence": [f"Timed out after {timeout}s"],
        }
    except Exception as err:
        elapsed = int((time.time() - start) * 1000)
        return {
            "status": "down",
            "summary": "Health probe failed",
            "latencyMs": elapsed,
            "evidence": [str(err)],
        }


def run_command_probe(system):
    command = system.get("command")
    timeout = int(system.get("timeoutSeconds", 15))

    if not RUN_COMMAND_PROBES:
        return {
            "status": "unknown",
            "summary": "Command probes disabled",
            "latencyMs": 0,
            "evidence": ["Set RUN_COMMAND_PROBES=true to enable execution"],
        }

    start = time.time()
    try:
        proc = subprocess.run(
            command,
            shell=True,
            timeout=timeout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        elapsed = int((time.time() - start) * 1000)
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()
        lines = []
        if stdout:
            lines.extend(stdout.splitlines()[:4])
        if stderr:
            lines.extend(stderr.splitlines()[:2])

        if proc.returncode == 0:
            status = "ok"
            summary = "Command succeeded"
        else:
            diagnostic_blob = "\n".join(lines).lower()
            runtime_config_errors = [
                "could not resolve hostname",
                "context \"rpi\": context not found",
                "bad owner or permissions on /root/.ssh/config",
                "permission denied (publickey",
                "connection refused",
            ]

            if any(token in diagnostic_blob for token in runtime_config_errors):
                status = "unknown"
                summary = "Runtime access not configured"
            else:
                status = "degraded"
                summary = f"Command exit {proc.returncode}"

        return {
            "status": status,
            "summary": summary,
            "latencyMs": elapsed,
            "evidence": lines,
        }
    except subprocess.TimeoutExpired:
        elapsed = int((time.time() - start) * 1000)
        return {
            "status": "down",
            "summary": "Command timeout",
            "latencyMs": elapsed,
            "evidence": [f"Timed out after {timeout}s"],
        }
    except Exception as err:
        elapsed = int((time.time() - start) * 1000)
        return {
            "status": "down",
            "summary": "Command probe failed",
            "latencyMs": elapsed,
            "evidence": [str(err)],
        }


def run_probe(system):
    probe_type = system.get("type")
    if probe_type == "http":
        return run_http_probe(system)
    if probe_type == "tcp":
        return run_tcp_probe(system)
    if probe_type == "command":
        return run_command_probe(system)
    if probe_type == "docker-health":
        return run_docker_health_probe(system)

    return {
        "status": "unknown",
        "summary": f"Unsupported probe type: {probe_type}",
        "latencyMs": 0,
        "evidence": [],
    }


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "time": utc_now_iso()})


@app.get("/api/status")
def api_status():
    cfg = load_probe_config()
    systems = []

    for system in cfg.get("systems", []):
        result = run_probe(system)
        systems.append(
            {
                "id": system.get("id"),
                "name": system.get("name"),
                "group": system.get("group", "General"),
                "type": system.get("type"),
                "status": result.get("status", "unknown"),
                "summary": result.get("summary", ""),
                "latencyMs": result.get("latencyMs", 0),
                "evidence": result.get("evidence", []),
                "checkedAt": utc_now_iso(),
            }
        )

    return jsonify(
        {
            "generatedAt": utc_now_iso(),
            "commandProbesEnabled": RUN_COMMAND_PROBES,
            "systems": systems,
            "counts": {
                "ok": len([s for s in systems if s["status"] == "ok"]),
                "degraded": len([s for s in systems if s["status"] == "degraded"]),
                "down": len([s for s in systems if s["status"] == "down"]),
                "unknown": len([s for s in systems if s["status"] == "unknown"]),
            },
        }
    )


@app.get("/api/portal-config")
def portal_config():
    return jsonify(
        {
            "public": {
                "lspTabEnabled": PUBLIC_LSP_TAB_ENABLED,
            },
            "internal": {
                "path": "/internal",
            },
            "home": {
                "target": HOME_VIEW_TARGET,
                "sourceUrl": resolve_home_source_url(),
            },
        }
    )


@app.get("/api/nip11-summary")
def nip11_summary():
    attempts = []

    for url in NIP11_SOURCE_URLS:
        started = time.time()
        try:
            code, payload = fetch_nip11(url)
            elapsed = int((time.time() - started) * 1000)

            return jsonify(
                {
                    "ok": True,
                    "checkedAt": utc_now_iso(),
                    "source": url,
                    "httpCode": code,
                    "latencyMs": elapsed,
                    "nip11": payload,
                    "attempts": attempts,
                }
            )
        except Exception as err:
            elapsed = int((time.time() - started) * 1000)
            attempts.append(
                {
                    "source": url,
                    "latencyMs": elapsed,
                    "error": str(err),
                }
            )

    return (
        jsonify(
            {
                "ok": False,
                "checkedAt": utc_now_iso(),
                "source": None,
                "nip11": {},
                "attempts": attempts,
            }
        ),
        502,
    )


@app.get("/")
def root():
    return send_from_directory(APP_DIR, "index.html")


@app.get("/internal")
def internal_root():
    return send_from_directory(APP_DIR, "internal-portal.html")


@app.get("/internal-portal.html")
def internal_page():
    return send_from_directory(APP_DIR, "internal-portal.html")


@app.get("/legacy.html")
def legacy():
    return send_from_directory(APP_DIR, "legacy.html")


@app.get("/home-dev.html")
def home_dev():
    return send_from_directory(APP_DIR, "home-dev.html")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8099"))
    app.run(host="0.0.0.0", port=port)
