#!/usr/bin/env bash
# Commands:
#   setup      one-time: create the Wine prefix, install Windows Python + the MT5 terminal
#   inspect    log in with the investor password and print a summary (changes nothing)
#   download   log in and save the account's history to /data/accounts/<login>.db
#   shell      a bash shell in the container (for debugging)
set -euo pipefail

PY_VERSION=3.11.9
PY_EXE='C:\Python311\python.exe'
MT5_DIR="$WINEPREFIX/drive_c/Program Files/MetaTrader 5"
MT5_WIN_PATH='C:\Program Files\MetaTrader 5\terminal64.exe'

start_display() {
  if ! pgrep -x Xvfb >/dev/null; then
    Xvfb :99 -screen 0 1280x1024x24 -nolisten tcp >/dev/null 2>&1 &
    sleep 2
  fi
}

wine_python() { wine "$PY_EXE" "$@"; }

setup() {
  start_display
  [ -f "$WINEPREFIX/system.reg" ] || { echo ">> creating the Wine prefix"; wineboot --init; wineserver -w; }

  if [ ! -f "$WINEPREFIX/drive_c/Python311/python.exe" ]; then
    echo ">> installing Windows Python $PY_VERSION"
    wget -q -O /tmp/python-setup.exe "https://www.python.org/ftp/python/$PY_VERSION/python-$PY_VERSION-amd64.exe"
    wine /tmp/python-setup.exe /quiet InstallAllUsers=1 TargetDir='C:\Python311' Include_test=0 Include_launcher=0
    wineserver -w
  fi

  if [ ! -f "$MT5_DIR/terminal64.exe" ]; then
    echo ">> installing the MetaTrader 5 terminal (this can take a few minutes)"
    wget -q -O /tmp/mt5setup.exe "https://download.mql5.com/cdn/web/metaquotes.software.corp/mt5/mt5setup.exe"
    wine /tmp/mt5setup.exe /auto &
    for _ in $(seq 1 90); do [ -f "$MT5_DIR/terminal64.exe" ] && break; sleep 5; done
    sleep 10
    wineserver -k || true   # the installer starts the terminal; stop it
    [ -f "$MT5_DIR/terminal64.exe" ] || { echo "MT5 install did not finish; run 'make mt5-setup' again" >&2; exit 1; }
  fi

  install_package
  echo ">> setup done"
}

install_package() {
  wine_python -m pip install --quiet --disable-pip-version-check --no-warn-script-location 'Z:\src'
}

run_cli() {
  start_display
  [ -f "$MT5_DIR/terminal64.exe" ] || { echo "Run 'make mt5-setup' first." >&2; exit 1; }
  install_package  # picks up code changes after an image rebuild
  export MT5_PATH="${MT5_PATH:-$MT5_WIN_PATH}"
  wine_python -m tradequery.cli "$@"
}

case "${1:-help}" in
  setup) setup ;;
  inspect|download) run_cli "$@" ;;
  shell) start_display; exec bash ;;
  *) sed -n '2,7p' "$0" ;;
esac
