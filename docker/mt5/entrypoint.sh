#!/usr/bin/env bash
# Commands:
#   setup      one-time: create the Wine prefix, install Windows Python + the MT5 terminal
#   inspect    log in with the investor password and print a summary (changes nothing)
#   download   log in and save the account's history to /data/accounts/<login>.db
#   shell      a bash shell in the container (for debugging)
set -euo pipefail

PY_VERSION=3.11.9
PY_EXE='C:\Python311\python.exe'
PROGRAM_FILES="$WINEPREFIX/drive_c/Program Files"
# Your broker's MT5 installer gives its release build and its server list; set
# MT5_INSTALLER_URL in .env. The generic MetaQuotes installer is the fallback.
MT5_INSTALLER_URL="${MT5_INSTALLER_URL:-https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe}"
TERMINAL_RECORD="$WINEPREFIX/.tradequery-terminal-dir"

# The folder (under Program Files) of the terminal to use: $MT5_TERMINAL_DIR, else
# the one setup installed, else the only broker terminal present, else "MetaTrader 5".
terminal_dir() {
  if [ -n "${MT5_TERMINAL_DIR:-}" ]; then echo "$MT5_TERMINAL_DIR"; return; fi
  if [ -f "$TERMINAL_RECORD" ]; then cat "$TERMINAL_RECORD"; return; fi
  local brokers
  brokers=$(cd "$PROGRAM_FILES" && for d in */; do [ -f "$d/terminal64.exe" ] && echo "${d%/}"; done | grep -vx "MetaTrader 5" || true)
  if [ "$(printf '%s\n' "$brokers" | grep -c .)" = 1 ]; then echo "$brokers"; else echo "MetaTrader 5"; fi
}

start_display() {
  if ! pgrep -x Xvfb >/dev/null; then
    Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp >/dev/null 2>&1 &
    sleep 2
  fi
}

wine_python() { wine "$PY_EXE" "$@"; }

# Without these overrides wineboot opens an "install Wine Mono / Gecko?" dialog,
# which nobody can click in a headless container, and hangs forever.
wineboot_quiet() {
  WINEDLLOVERRIDES="mscoree=;mshtml=" timeout 600 wineboot "$@"
  timeout 300 wineserver -w
  wine --version > "$VERSION_FILE"
}

# A complete prefix has both the 64-bit and the 32-bit (syswow64) system DLLs.
VERSION_FILE="$WINEPREFIX/.tradequery-wine-version"
prefix_complete() { [ -f "$WINEPREFIX/drive_c/windows/syswow64/kernel32.dll" ]; }

# Make sure the prefix exists, is complete, and matches this image's Wine version.
ensure_prefix() {
  if ! prefix_complete; then
    if [ -f "$WINEPREFIX/system.reg" ]; then
      echo ">> the Wine prefix is incomplete (an earlier setup was interrupted); rebuilding it"
      find "$WINEPREFIX" -mindepth 1 -delete  # the prefix folder is the volume mount, so keep it
    fi
    echo ">> creating the Wine prefix"
    wineboot_quiet --init
    prefix_complete || { echo "Wine prefix creation failed; run 'make mt5-setup' again" >&2; exit 1; }
  elif [ "$(cat "$VERSION_FILE" 2>/dev/null)" != "$(wine --version)" ]; then
    echo ">> upgrading the Wine prefix to $(wine --version) (Python and MT5 stay installed)"
    wineboot_quiet --update
  fi
}

setup() {
  start_display
  ensure_prefix

  if [ ! -f "$WINEPREFIX/drive_c/Python311/python.exe" ]; then
    echo ">> installing Windows Python $PY_VERSION"
    wget -q -O /tmp/python-setup.exe "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-amd64.exe"
    wine /tmp/python-setup.exe /quiet InstallAllUsers=1 TargetDir='C:\Python311' Include_test=0 Include_launcher=0
    wineserver -w
  fi

  if [ ! -f "$PROGRAM_FILES/$(terminal_dir)/terminal64.exe" ]; then
    echo ">> installing the MT5 terminal from $MT5_INSTALLER_URL (this can take a few minutes)"
    wget -q -O /tmp/mt5setup.exe "$MT5_INSTALLER_URL"
    wine /tmp/mt5setup.exe /auto &
    local found=""
    for _ in $(seq 1 90); do
      found=$(find "$PROGRAM_FILES" -maxdepth 2 -name terminal64.exe -newer /tmp/mt5setup.exe | head -1)
      [ -n "$found" ] && break; sleep 5
    done
    sleep 10
    wineserver -k || true   # the installer starts the terminal; stop it
    [ -n "$found" ] || { echo "MT5 install did not finish; run 'make mt5-setup' again" >&2; exit 1; }
    basename "$(dirname "$found")" > "$TERMINAL_RECORD"
  fi
  echo ">> terminal: C:\\Program Files\\$(terminal_dir)"

  install_package
  echo ">> setup done"
}

install_package() {
  wine_python -m pip install --quiet --disable-pip-version-check --no-warn-script-location 'Z:\src'
}

authorized_count() {
  cat "$1"/logs/*.log 2>/dev/null | iconv -f UTF-16LE -t UTF-8 2>/dev/null | grep -c "authorized on" || true
}

# The Python library deadlocks against a terminal that is not logged in yet (it
# waits for a "ready" state the terminal only reaches after login). So start the
# terminal already logged in, from a startup config built from the .env values,
# and attach Python afterwards. The config file lives in the container's /tmp,
# is readable only by this user, and is deleted as soon as the terminal is in.
start_terminal_logged_in() {
  local dir="$1" win_exe="$2" before i
  if [ -z "${MT5_LOGIN:-}" ] || [ -z "${MT5_INVESTOR_PASSWORD:-}" ] || [ -z "${MT5_SERVER:-}" ]; then
    return 0  # the CLI reports what is missing
  fi
  before=$(authorized_count "$dir")
  ( umask 077
    printf '[Common]\r\nLogin=%s\r\nPassword=%s\r\nServer=%s\r\n' \
      "$MT5_LOGIN" "$MT5_INVESTOR_PASSWORD" "$MT5_SERVER" | iconv -f UTF-8 -t UTF-16 > /tmp/tq-start.ini )
  wine "$win_exe" '/config:Z:\tmp\tq-start.ini' > /dev/null 2>&1 &
  for i in $(seq 1 36); do
    sleep 5
    if [ "$(authorized_count "$dir")" -gt "$before" ]; then
      rm -f /tmp/tq-start.ini
      echo ">> terminal logged in (${i}x5s)"
      return 0
    fi
  done
  rm -f /tmp/tq-start.ini
  echo "The terminal did not log in within 3 minutes. Last journal lines:" >&2
  cat "$dir"/logs/*.log 2>/dev/null | iconv -f UTF-16LE -t UTF-8 2>/dev/null | tr -d '\r' | tail -5 >&2
  exit 1
}

run_cli() {
  start_display
  local name dir
  name=$(terminal_dir)
  dir="$PROGRAM_FILES/$name"
  [ -f "$dir/terminal64.exe" ] || { echo "No MT5 terminal in $dir. Run 'make mt5-setup' first." >&2; exit 1; }
  ensure_prefix  # upgrades the prefix after a Wine version change
  install_package  # picks up code changes after an image rebuild
  export MT5_PATH="C:\\Program Files\\$name\\terminal64.exe"
  start_terminal_logged_in "$dir" "$MT5_PATH"
  local status=0
  wine_python -m tradequery.cli "$@" || status=$?
  wineserver -k || true
  return "$status"
}

case "${1:-help}" in
  setup) setup ;;
  inspect|download) run_cli "$@" ;;
  shell) start_display; exec bash ;;
  *) sed -n '2,7p' "$0" ;;
esac
