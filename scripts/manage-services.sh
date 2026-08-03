#!/usr/bin/env bash
set -euo pipefail

# Manage local runtime processes for testing from the current repository.
# Defaults can be overridden with TUXWS_SERVICES env var, e.g.:
# TUXWS_SERVICES="tuxwsmaker-web tuxwsmaker-worker" ./scripts/manage-services.sh restart

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
RUN_DIR="${ROOT_DIR}/.run"
LOG_DIR="${RUN_DIR}/logs"

DEFAULT_SERVICES=(
  "tuxwsmaker-web"
  "tuxwsmaker-worker"
  "tuxwsmaker-beat"
  "redis"
)

START_ORDER=(
  "redis"
  "tuxwsmaker-web"
  "tuxwsmaker-worker"
  "tuxwsmaker-beat"
)

usage() {
  cat <<'EOF'
Usage:
  ./scripts/manage-services.sh <start|stop|restart|status> [service ...]

Examples:
  ./scripts/manage-services.sh restart
  ./scripts/manage-services.sh status tuxwsmaker-web tuxwsmaker-worker
  TUXWS_SERVICES="tuxwsmaker-web tuxwsmaker-worker redis" ./scripts/manage-services.sh start

Environment:
  TUXWS_WEB_PORT    Default: 8000
  TUXWS_WEB_HOST    Default: 0.0.0.0
  TUXWS_REDIS_PORT  Default: 6379
  TUXWS_WS_PATH     Default: /ws/updates/
EOF
}

if [[ $# -lt 1 ]]; then
  usage
  exit 1
fi

action="$1"
shift || true

case "$action" in
  start|stop|restart|status) ;;
  *)
    echo "ERROR: Unsupported action '$action'"
    usage
    exit 1
    ;;
esac

if [[ $# -gt 0 ]]; then
  services=("$@")
elif [[ -n "${TUXWS_SERVICES:-}" ]]; then
  read -r -a services <<<"$TUXWS_SERVICES"
else
  services=("${DEFAULT_SERVICES[@]}")
fi

if [[ ${#services[@]} -eq 0 ]]; then
  echo "ERROR: No services selected"
  exit 1
fi

mkdir -p "${RUN_DIR}" "${LOG_DIR}"

service_command() {
  local svc="$1"
  local web_port="${TUXWS_WEB_PORT:-8000}"
  local web_host="${TUXWS_WEB_HOST:-0.0.0.0}"
  local redis_port="${TUXWS_REDIS_PORT:-6379}"

  case "$svc" in
    tuxwsmaker-web)
      echo ". .venv/bin/activate && daphne --http-timeout 3600 -b ${web_host} -p ${web_port} config.asgi:application"
      ;;
    tuxwsmaker-worker)
      echo ". .venv/bin/activate && celery -A config worker -l info"
      ;;
    tuxwsmaker-beat)
      echo ". .venv/bin/activate && celery -A config beat -l info"
      ;;
    redis)
      echo "redis-server --port ${redis_port} --save '' --appendonly no"
      ;;
    *)
      return 1
      ;;
  esac
}

require_prereqs() {
  local svc="$1"
  if [[ ! -f "${ROOT_DIR}/manage.py" ]]; then
    echo "ERROR: manage.py not found in ${ROOT_DIR}. Run script from repository checkout."
    exit 1
  fi

  if [[ "$svc" == tuxwsmaker-web || "$svc" == tuxwsmaker-worker || "$svc" == tuxwsmaker-beat ]]; then
    if [[ ! -x "${ROOT_DIR}/.venv/bin/python" ]]; then
      echo "ERROR: ${ROOT_DIR}/.venv not found. Create venv and install requirements first."
      exit 1
    fi
  fi

  if [[ "$svc" == redis ]] && ! command -v redis-server >/dev/null 2>&1; then
    echo "ERROR: redis-server not found in PATH. Install Redis or omit redis from service selection."
    exit 1
  fi
}

needs_django_migration() {
  local svc
  for svc in "${services[@]}"; do
    case "$svc" in
      tuxwsmaker-web|tuxwsmaker-worker|tuxwsmaker-beat)
        return 0
        ;;
    esac
  done
  return 1
}

ordered_services() {
  local ordered=()
  local candidate
  local target

  for target in "${START_ORDER[@]}"; do
    for candidate in "${services[@]}"; do
      if [[ "$candidate" == "$target" ]]; then
        ordered+=("$candidate")
      fi
    done
  done

  printf '%s\n' "${ordered[@]}"
}

run_migrations() {
  echo "==> running Django migrations"
  (
    cd "${ROOT_DIR}"
    bash -lc ". .venv/bin/activate && python manage.py migrate --noinput"
  )
}

run_build_state_reconcile() {
  echo "==> reconciling stale build task states"
  (
    cd "${ROOT_DIR}"
    bash -lc ". .venv/bin/activate && python manage.py reconcile_build_states"
  )
}

wait_for_redis() {
  local redis_port="${TUXWS_REDIS_PORT:-6379}"

  echo "==> waiting for redis on 127.0.0.1:${redis_port}"
  for _ in $(seq 1 30); do
    if redis-cli -h 127.0.0.1 -p "$redis_port" ping >/dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done

  echo "ERROR: redis did not become ready on 127.0.0.1:${redis_port}"
  exit 1
}

pid_file() { echo "${RUN_DIR}/$1.pid"; }
log_file() { echo "${LOG_DIR}/$1.log"; }

get_listening_ports() {
  local pid="$1"

  if ! command -v ss >/dev/null 2>&1; then
    echo "unknown (ss missing)"
    return
  fi

  local pid_list
  pid_list="${pid}"
  if command -v pgrep >/dev/null 2>&1; then
    local frontier
    frontier="${pid}"
    while [[ -n "$frontier" ]]; do
      local next=""
      for p in $frontier; do
        local children
        children="$(pgrep -P "$p" 2>/dev/null || true)"
        if [[ -n "$children" ]]; then
          pid_list="${pid_list} ${children}"
          next="${next} ${children}"
        fi
      done
      frontier="${next}"
    done
  fi

  local ports
  ports="$({ ss -H -ltnup 2>/dev/null || true; } | awk -v pids="$pid_list" '
    BEGIN {
      n = split(pids, arr, /[[:space:]]+/)
      for (i = 1; i <= n; i++) {
        if (arr[i] != "") {
          watch[arr[i]] = 1
        }
      }
    }
    {
      matched = 0
      for (p in watch) {
        if (index($0, "pid=" p)) {
          matched = 1
          break
        }
      }
      if (matched) {
        split($5, a, ":")
        port = a[length(a)]
        if (port ~ /^[0-9]+$/) {
          seen[port] = 1
        }
      }
    }
    END {
      out = "";
      for (k in seen) {
        out = out (out ? "," : "") k;
      }
      print out;
    }
  ')"

  if [[ -z "$ports" ]]; then
    echo "-"
  else
    echo "$ports"
  fi
}

is_running() {
  local svc="$1"
  local pidf
  pidf="$(pid_file "$svc")"

  if [[ ! -f "$pidf" ]]; then
    return 1
  fi

  local pid
  pid="$(cat "$pidf")"
  if [[ -z "$pid" ]] || ! kill -0 "$pid" 2>/dev/null; then
    rm -f "$pidf"
    return 1
  fi
  return 0
}

start_service() {
  local svc="$1"
  require_prereqs "$svc"

  if is_running "$svc"; then
    echo "==> ${svc}: already running (pid $(cat "$(pid_file "$svc")"))"
    return
  fi

  local cmd
  if ! cmd="$(service_command "$svc")"; then
    echo "ERROR: Unknown service: $svc"
    exit 1
  fi

  local logf
  logf="$(log_file "$svc")"

  echo "==> starting ${svc}"
  (
    cd "${ROOT_DIR}"
    nohup bash -lc "$cmd" >>"$logf" 2>&1 &
    echo $! >"$(pid_file "$svc")"
  )

  sleep 1
  if ! is_running "$svc"; then
    echo "ERROR: Failed to start ${svc}. Check log: $logf"
    exit 1
  fi
}

stop_service() {
  local svc="$1"
  if ! is_running "$svc"; then
    echo "==> ${svc}: not running"
    return
  fi

  local pid
  pid="$(cat "$(pid_file "$svc")")"
  echo "==> stopping ${svc} (pid ${pid})"
  kill "$pid" 2>/dev/null || true

  for _ in $(seq 1 10); do
    if ! kill -0 "$pid" 2>/dev/null; then
      rm -f "$(pid_file "$svc")"
      return
    fi
    sleep 1
  done

  kill -9 "$pid" 2>/dev/null || true
  rm -f "$(pid_file "$svc")"
}

status_service() {
  local svc="$1"
  local pidf
  pidf="$(pid_file "$svc")"
  local logf
  logf="$(log_file "$svc")"

  if is_running "$svc"; then
    local pid
    pid="$(cat "$pidf")"
    local ports
    ports="$(get_listening_ports "$pid")"
    printf '%-24s running (pid %s)  ports=%s  log=%s\n' "$svc" "$pid" "$ports" "$logf"
    if [[ "$svc" == "tuxwsmaker-web" ]]; then
      local web_port
      local ws_path
      web_port="${TUXWS_WEB_PORT:-8000}"
      ws_path="${TUXWS_WS_PATH:-/ws/updates/}"
      printf '  %-22s %s\n' "web:" "http://127.0.0.1:${web_port}/"
      printf '  %-22s %s\n' "websocket:" "ws://127.0.0.1:${web_port}${ws_path}"
    fi
  else
    printf '%-24s stopped            ports=-  log=%s\n' "$svc" "$logf"
  fi
}

mapfile -t startup_services < <(ordered_services)

for svc in "${startup_services[@]}"; do
  case "$action" in
    start)
      start_service "$svc"
      if [[ "$svc" == "redis" ]]; then
        wait_for_redis
      fi
      ;;
    stop) stop_service "$svc" ;;
    restart)
      stop_service "$svc"
      ;;
    status) status_service "$svc" ;;
  esac
done

if [[ "$action" == "restart" ]] && needs_django_migration; then
  run_migrations
fi

if [[ "$action" == "restart" ]]; then
  mapfile -t ordered_restart_services < <(ordered_services)
  for svc in "${ordered_restart_services[@]}"; do
    start_service "$svc"
    if [[ "$svc" == "redis" ]]; then
      wait_for_redis
    fi
  done
fi

if [[ "$action" == "start" || "$action" == "restart" ]]; then
  if needs_django_migration; then
    run_build_state_reconcile
  fi
fi

if [[ "$action" != "status" ]]; then
  echo
  for svc in "${services[@]}"; do
    status_service "$svc"
  done
fi
