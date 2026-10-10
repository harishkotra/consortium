#!/usr/bin/env bash
# Start / stop / inspect the whole consortium stack.
#
#   ./run.sh start      # mcp_server.py, vendor_agent.py, buyer_agent.py, then the observer
#   ./run.sh stop
#   ./run.sh status
#   ./run.sh restart
#
# Ports come from .env (defaults: MCP 8123, vendor 8101, buyer 8102, observer 8501).
set -uo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-.venv/bin/python}
LOGS=logs
ORDER=(mcp vendor buyer observer)

if [[ ! -x $PY ]]; then
  echo "no $PY — create the venv first:"
  echo "  uv venv --python 3.12 .venv && uv pip install -p .venv/bin/python -r requirements.txt"
  exit 1
fi

wait_port() { # host port timeout
  local host=$1 port=$2 deadline=$(( SECONDS + ${3:-45} ))
  while (( SECONDS < deadline )); do
    if (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then exec 3>&- 3<&-; return 0; fi
    sleep 0.4
  done
  return 1
}

start() {
  mkdir -p "$LOGS"
  local spec name cmd host port
  for spec in \
    "mcp|$PY mcp_server.py|127.0.0.1|8123" \
    "vendor|$PY vendor_agent.py|127.0.0.1|8101" \
    "buyer|$PY buyer_agent.py|127.0.0.1|8102" \
    "observer|$PY -m streamlit run observer.py --server.port 8501 --server.address 127.0.0.1 --server.headless true --browser.gatherUsageStats false|127.0.0.1|8501"; do
    IFS='|' read -r name cmd host port <<< "$spec"
    if [[ -f "$LOGS/$name.pid" ]] && kill -0 "$(cat "$LOGS/$name.pid")" 2>/dev/null; then
      echo "  $name already running (pid $(cat "$LOGS/$name.pid"))"
      continue
    fi
    # An open port tells us nothing about who owns it — refuse rather than shadow another process.
    if (exec 3<>"/dev/tcp/$host/$port") 2>/dev/null; then
      exec 3>&- 3<&-
      echo "  $name NOT started: :$port is already held by a process this script does not own"
      echo "      find it with: lsof -nP -iTCP:$port -sTCP:LISTEN"
      return 1
    fi
    nohup $cmd > "$LOGS/$name.log" 2>&1 &
    pid=$!
    echo "$pid" > "$LOGS/$name.pid"
    if wait_port "$host" "$port" 60 && kill -0 "$pid" 2>/dev/null; then
      echo "  $name up (pid $pid, :$port)"
    else
      echo "  $name FAILED (pid $pid, :$port) — last lines of $LOGS/$name.log:"
      tail -12 "$LOGS/$name.log" | sed 's/^/      /'
      return 1
    fi
  done
  echo
  echo "observer: http://127.0.0.1:8501"
}

stop() {
  local name pid
  for name in "${ORDER[@]}"; do
    [[ -f "$LOGS/$name.pid" ]] || continue
    pid=$(cat "$LOGS/$name.pid")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" 2>/dev/null
      echo "  stopped $name (pid $pid)"
    fi
    rm -f "$LOGS/$name.pid"
  done
}

status() {
  local name url body
  for name in "${ORDER[@]}"; do
    case $name in
      mcp) url="http://127.0.0.1:8123/mcp" ;;
      vendor) url="http://127.0.0.1:8101/health" ;;
      buyer) url="http://127.0.0.1:8102/health" ;;
      observer) url="http://127.0.0.1:8501/_stcore/health" ;;
    esac
    body=$(curl -s --max-time 3 "$url" 2>/dev/null | head -c 220)
    printf "  %-9s %s\n" "$name" "${body:-<no answer at $url>}"
  done
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  restart) stop; sleep 1; start ;;
  status) status ;;
  tail) tail -n 40 -f $LOGS/*.log ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
