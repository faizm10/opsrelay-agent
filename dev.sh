#!/usr/bin/env bash
# Start the OpsRelay uptime API, the dashboard and the AI agent worker from one terminal.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOG_DIR="$ROOT/.dev/logs"
PYTHON="${PYTHON:-}"
if [[ -z "$PYTHON" ]]; then
  if command -v python3.12 >/dev/null 2>&1; then
    PYTHON=python3.12
  else
    PYTHON=python3
  fi
fi

usage() {
  cat <<'EOF'
Usage: ./dev.sh [up|setup|logs|reset|help]

  setup  Install the Python and Node dependencies for the dashboard and the agent.
  up     Start both servers and the AI agent worker (default). Press Ctrl+C to stop them all.
  logs   Follow the server and agent logs in .dev/logs/.
  reset  Clear incidents in Atlas, the dev logs and old agent/* branches (asks first).

Set PYTHON=python3.12 to choose a Python interpreter during setup.
Set DEV_PORT_OFFSET=100 to shift every port when the defaults are occupied.
Set DEV_AGENT=0 to start the servers without the AI agent worker.
EOF
}

require_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    echo "Missing $1. Install it and try again." >&2
    exit 1
  fi
}

check_versions() {
  require_command "$PYTHON"
  require_command node
  require_command npm
  require_command curl
  require_command pgrep
  "$PYTHON" -c 'import sys; sys.exit(0 if (3, 11) <= sys.version_info[:2] < (3, 15) else 1)' || {
    echo "Python 3.11–3.14 is required (3.12 recommended)." >&2
    exit 1
  }
  node -e 'const [major, minor] = process.versions.node.split(".").map(Number); process.exit(major > 18 || (major === 18 && minor >= 18) ? 0 : 1)' || {
    echo "Node.js 18.18+ is required (20 recommended)." >&2
    exit 1
  }
}

setup() {
  check_versions
  local ca_file
  echo "Installing the uptime API..."
  "$PYTHON" -m venv "$ROOT/admin-dashboard/backend/.venv"
  "$ROOT/admin-dashboard/backend/.venv/bin/python" -m pip install -r "$ROOT/admin-dashboard/backend/requirements.txt"
  echo "Installing the dashboard..."
  (cd "$ROOT/admin-dashboard/web" && npm ci)
  echo "Installing the AI agent..."
  "$PYTHON" -m venv "$ROOT/agents/.venv"
  "$ROOT/agents/.venv/bin/python" -m pip install -r "$ROOT/requirements.txt"
  if has_mongodb_uri; then
    ca_file="$(agent_ca_file)"
    (cd "$ROOT" && env ${ca_file:+SSL_CERT_FILE="$ca_file"} "$ROOT/agents/.venv/bin/python" -m agents.orchestrator setup) \
      || echo "Dependencies are installed; the agent's MongoDB collections are not set up yet (see above)."
  else
    echo "Add MONGODB_URI to .env, then run ./dev.sh setup again to prepare the agent's MongoDB collections."
  fi
  echo "Setup complete. Run ./dev.sh to start the servers and the AI agent."
}

# Print the CA bundle for the agent's Python. python.org builds on macOS ship without
# CA certificates, so TLS to Atlas fails; use certifi's bundle (installed with
# requests) unless SSL_CERT_FILE is already set.
agent_ca_file() {
  echo "${SSL_CERT_FILE:-$("$ROOT/agents/.venv/bin/python" -c 'import certifi; print(certifi.where())' 2>/dev/null || true)}"
}

has_mongodb_uri() {
  [[ -f "$ROOT/.env" ]] && grep -qE '^[[:space:]]*MONGODB_URI[[:space:]]*=[[:space:]]*[^[:space:]]' "$ROOT/.env"
}

# Print why the agent worker cannot start, or nothing when it can.
agent_skip_reason() {
  if [[ "${DEV_AGENT:-1}" == "0" ]]; then
    echo "DEV_AGENT=0"
  elif [[ ! -x "$ROOT/agents/.venv/bin/python" ]]; then
    echo "agent environment missing; run ./dev.sh setup"
  elif ! has_mongodb_uri; then
    echo "MONGODB_URI is not set in .env"
  fi
}

check_dependencies() {
  local missing=0
  if [[ ! -x "$ROOT/admin-dashboard/backend/.venv/bin/python" ]]; then
    echo "Missing the uptime API environment." >&2
    missing=1
  fi
  if [[ ! -x "$ROOT/admin-dashboard/web/node_modules/.bin/next" ]]; then
    echo "Missing the dashboard dependencies." >&2
    missing=1
  fi
  if ((missing)); then
    echo "Run ./dev.sh setup first." >&2
    exit 1
  fi
}

check_port() {
  local port="$1"
  if ! "$PYTHON" - "$port" <<'PY'
import socket
import sys

with socket.socket() as sock:
    try:
        sock.bind(("127.0.0.1", int(sys.argv[1])))
    except OSError:
        sys.exit(1)
PY
  then
    echo "Port $port is already in use. Stop that server before running ./dev.sh." >&2
    exit 1
  fi
}

NAMES=()
PIDS=()

start() {
  local name="$1" directory="$2"
  shift 2
  (cd "$directory" && exec "$@") >"$LOG_DIR/$name.log" 2>&1 &
  NAMES+=("$name")
  PIDS+=("$!")
}

stop_tree() {
  local pid="$1" child
  while read -r child; do
    [[ -n "$child" ]] && stop_tree "$child"
  done < <(pgrep -P "$pid" 2>/dev/null || true)
  kill -TERM "$pid" 2>/dev/null || true
}

cleanup() {
  trap - EXIT INT TERM
  if ((${#PIDS[@]})); then
    echo "Stopping all servers..."
    local pid
    for pid in "${PIDS[@]}"; do stop_tree "$pid"; done
    for pid in "${PIDS[@]}"; do wait "$pid" 2>/dev/null || true; done
  fi
}

wait_ready() {
  local name="$1" url="$2" pid="$3" attempt
  for ((attempt=0; attempt<60; attempt++)); do
    if ! kill -0 "$pid" 2>/dev/null; then
      echo "$name exited during startup. Recent log output:" >&2
      tail -n 20 "$LOG_DIR/$name.log" >&2
      exit 1
    fi
    if curl -fsS --max-time 2 -o /dev/null "$url" 2>/dev/null; then
      echo "Ready: $name  $url"
      return
    fi
    sleep 1
  done
  echo "$name did not become ready within 60 seconds. Recent log output:" >&2
  tail -n 20 "$LOG_DIR/$name.log" >&2
  exit 1
}

up() {
  check_versions
  check_dependencies
  local offset="${DEV_PORT_OFFSET:-0}" port
  if [[ ! "$offset" =~ ^[0-9]+$ ]] || ((10#$offset > 57235)); then
    echo "DEV_PORT_OFFSET must be an integer from 0 to 57235." >&2
    exit 1
  fi
  local relay_web=$((3300 + offset)) relay_api=$((8300 + offset))
  for port in "$relay_web" "$relay_api"; do check_port "$port"; done
  mkdir -p "$LOG_DIR"
  trap cleanup EXIT
  trap 'exit 0' INT TERM

  start opsrelay-api "$ROOT/admin-dashboard/backend" env OPSRELAY_ENV=development "$ROOT/admin-dashboard/backend/.venv/bin/python" -m uptime serve --reload --port "$relay_api"
  start opsrelay-web "$ROOT/admin-dashboard/web" env OPSRELAY_UPTIME_API_URL="http://127.0.0.1:$relay_api" "$ROOT/admin-dashboard/web/node_modules/.bin/next" dev -p "$relay_web"
  wait_ready opsrelay-api "http://127.0.0.1:$relay_api/api/v1/health" "${PIDS[0]}"
  wait_ready opsrelay-web "http://127.0.0.1:$relay_web" "${PIDS[1]}"

  echo "OpsRelay is running: http://localhost:$relay_web"

  # The agent watches the uptime incidents in MongoDB, then works on outages.
  # It is optional: the servers keep running if it stops.
  local agent=-1 skip ca_file
  skip="$(agent_skip_reason)"
  if [[ -z "$skip" ]]; then
    ca_file="$(agent_ca_file)"
    start agent-worker "$ROOT" env PYTHONUNBUFFERED=1 ${ca_file:+SSL_CERT_FILE="$ca_file"} "$ROOT/agents/.venv/bin/python" -m agents.orchestrator worker
    agent=$((${#PIDS[@]} - 1))
    echo "AI agent worker started. Log: $LOG_DIR/agent-worker.log"
  else
    echo "AI agent worker not started ($skip)."
  fi

  echo "Logs: $LOG_DIR"
  echo "Press Ctrl+C to stop them. In another terminal, run ./dev.sh logs to follow output."
  local i code
  while true; do
    for ((i=0; i<${#PIDS[@]}; i++)); do
      if ! kill -0 "${PIDS[i]}" 2>/dev/null; then
        code=0
        wait "${PIDS[i]}" || code=$?
        echo "${NAMES[i]} exited (status $code). Recent log output:" >&2
        tail -n 20 "$LOG_DIR/${NAMES[i]}.log" >&2
        if ((i == agent)); then
          echo "The demo servers are still running without the AI agent. Restart ./dev.sh to try again." >&2
          unset 'PIDS[i]' 'NAMES[i]'
          PIDS=("${PIDS[@]}")
          NAMES=("${NAMES[@]}")
          agent=-1
          break
        fi
        exit 1
      fi
    done
    sleep 1
  done
}

case "${1:-up}" in
  up) up ;;
  setup) setup ;;
  logs)
    if ! compgen -G "$LOG_DIR/*.log" >/dev/null; then
      echo "No logs yet. Run ./dev.sh first." >&2
      exit 1
    fi
    tail -n 20 -f "$LOG_DIR"/*.log
    ;;
  reset) cd "$ROOT" && exec "$ROOT/agents/.venv/bin/python" -m agents.reset "${@:2}" ;;
  help|-h|--help) usage ;;
  *) usage >&2; exit 2 ;;
esac
