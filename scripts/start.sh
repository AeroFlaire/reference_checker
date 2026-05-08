#!/usr/bin/env bash
# Single-command launcher for the reference checker.
#
# This script:
#   1. checks Docker is installed
#   2. on first run, creates a .env file and asks the user to fill it in
#   3. runs `docker compose up -d` to start GROBID + the Flask app
#   4. waits for both services to be healthy
#   5. prints the URL to open in the browser
#
# Re-running it after the first time just brings the stack back up.
set -euo pipefail

cd "$(dirname "$0")/.."

# --- 1. Check Docker ---
if ! command -v docker &>/dev/null; then
    cat <<EOF >&2

ERROR: Docker is not installed.

Install Docker Desktop:
  https://www.docker.com/products/docker-desktop/

Then re-run this script.
EOF
    exit 1
fi

if ! docker info &>/dev/null; then
    echo "ERROR: Docker is installed but the daemon isn't running." >&2
    echo "Start Docker Desktop, then re-run this script." >&2
    exit 1
fi

if ! docker compose version &>/dev/null; then
    echo "ERROR: 'docker compose' (v2) is required. Update Docker Desktop." >&2
    exit 1
fi

# --- 2. First-run config ---
if [[ ! -f .env ]]; then
    cp env.example .env
    cat <<EOF

A new .env file has been created at:
   $(pwd)/.env

Please open it and set OPENALEX_EMAIL to your email address (any valid email works,
this gives you faster rate limits on the academic databases). Then re-run start.sh.

EOF
    if command -v "${EDITOR:-}" &>/dev/null; then
        "${EDITOR}" .env
    elif command -v nano &>/dev/null; then
        nano .env
    elif command -v vi &>/dev/null; then
        vi .env
    fi
    exit 0
fi

# --- 3. Start the stack ---
echo "Starting GROBID + reference checker..."
echo "  (first run downloads ~2GB GROBID image; subsequent runs are fast)"
echo
docker compose up -d --build

# --- 4. Wait for health ---
echo
echo "Waiting for GROBID to finish loading (Java + ML models, ~30-60s)..."
for i in $(seq 1 60); do
    if curl -fsS http://localhost:8070/api/isalive &>/dev/null; then
        echo "  ✓ GROBID is up"
        break
    fi
    printf "."
    sleep 2
done
echo

for i in $(seq 1 30); do
    if curl -fsS http://localhost:5000/api/health &>/dev/null; then
        echo "  ✓ Reference checker app is up"
        break
    fi
    printf "."
    sleep 1
done
echo

# --- 5. Print URL ---
cat <<EOF

═════════════════════════════════════════════════════════════════
  ✓ Reference checker is running.

  Web UI:        http://localhost:5000
  Health check:  http://localhost:5000/api/health

  To stop:       docker compose down
  View logs:     docker compose logs -f
  Update:        docker compose pull && docker compose up -d --build

  For batch CLI mode on 600 PDFs, see README.md → "Batch mode".
═════════════════════════════════════════════════════════════════

EOF
