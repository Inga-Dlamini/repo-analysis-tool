#!/usr/bin/env bash
# Start the Repo Analysis Tool server.
#
#   ./run.sh              -> http://127.0.0.1:8000
#   PORT=9000 ./run.sh    -> http://127.0.0.1:9000
#   HOST=0.0.0.0 ./run.sh -> listen on all interfaces
set -euo pipefail

cd "$(dirname "$0")"

if ! command -v python3 >/dev/null 2>&1; then
  echo "error: python3 not found" >&2
  exit 1
fi

exec python3 -m backend.app
