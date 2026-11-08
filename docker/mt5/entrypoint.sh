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
  ensure_prefix  # upgrades the prefix after a Wine version change
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
