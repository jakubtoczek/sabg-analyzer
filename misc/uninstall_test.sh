#!/usr/bin/env bash
# Checks what uninstall.bat actually deletes, in a sandbox: it copies the script
# with the two roots pointed at a temp folder, the Desktop-shortcut step removed
# and the pauses stripped, then answers its prompts.  Run from Git Bash.
#
# The answers are preset as variables rather than piped in: cmd's `set /p` reads
# a piped stream in chunks and throws away what it did not use, so the second
# prompt would always see EOF.
set -e
cd "$(dirname "$0")/.."
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
W=$(cygpath -w "$T")

mk() {   # $1 = shared env names to create, $2 = non-empty for a private install
  rm -rf "$T/shared" "$T/private"
  for e in $1; do mkdir -p "$T/shared/envs/$e"; done
  [ -n "$1" ] && mkdir -p "$T/shared/python" && touch "$T/shared/uv.exe"
  [ -n "$2" ] && mkdir -p "$T/private" && touch "$T/private/uv.exe"
  return 0
}
run() {  # $1 = answer to the menu / Y prompt, $2 = answer to the ALL prompt
  { printf '@echo off\nsetlocal enabledelayedexpansion\nset "SHARED_ROOT=%s\shared"\nset "PRIVATE_ROOT=%s\private"\nset "CHOICE=%s"\nset "CONFIRM=%s"\n' \
           "$W" "$W" "$1" "${2:-$1}"
    sed -e '/^@echo off/d' -e '/^setlocal/d' \
        -e '/^set "SHARED_ROOT=/d' -e '/^set "PRIVATE_ROOT=/d' \
        -e '/set \/p /d' \
        -e '/^powershell -NoProfile/,/Remove-Item \$lnk -Force/d' \
        -e '/^ *pause$/d' uninstall.bat
  } > "$T/u.bat"
  cmd //c "$W\u.bat" 2>&1 | tr -d '\r'
}
gone() { [ -e "$T/$1" ] && { echo "FAIL: $1 should be gone"; exit 1; }; return 0; }
kept() { [ -e "$T/$1" ] || { echo "FAIL: $1 should be kept"; exit 1; }; return 0; }

mk "" ""
run "" | grep -q "Nothing to remove" || { echo "FAIL: empty case"; exit 1; }
echo "ok  nothing-installed"

mk "sabg_analyzer other_app" ""
run 1 >/dev/null
gone shared/envs/sabg_analyzer; kept shared/envs/other_app; kept shared/uv.exe
echo "ok  shared-app-only"

mk "sabg_analyzer other_app" ""
out=$(run 2 ALL)
[[ $out == *other_app* ]] || { echo "FAIL: option 2 must name the other app"; exit 1; }
gone shared
echo "ok  shared-remove-all"

mk "sabg_analyzer other_app" ""
run 2 nope >/dev/null
kept shared/envs/sabg_analyzer; kept shared/envs/other_app
echo "ok  shared-remove-all-declined"

mk "sabg_analyzer" ""
run x >/dev/null
kept shared/envs/sabg_analyzer
echo "ok  cancelled"

mk "sabg_analyzer" 1          # a private install wins: that folder is entirely ours
run Y >/dev/null
gone private; kept shared/envs/sabg_analyzer
echo "ok  private-whole-folder"

echo "uninstall behaviour OK"
