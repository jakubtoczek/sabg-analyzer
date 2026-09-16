#!/usr/bin/env bash
# Checks where SABG_Analyzer.bat decides to put its runtime, without downloading one:
# it copies the launcher up to the end of the env block, points the two roots at
# a sandbox, and prints what came out.  Run from Git Bash.
set -e
cd "$(dirname "$0")/.."
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
W=$(cygpath -w "$T")
n=$(grep -n 'UV_NO_MODIFY_PATH' SABG_Analyzer.bat | cut -d: -f1)
{ printf '@echo off\r\nset "SHARED_ROOT=%s\shared"\r\nset "PRIVATE_ROOT=%s\private"\r\n' "$W" "$W"
  sed -n "1,${n}p" SABG_Analyzer.bat | sed -e '/set "SHARED_ROOT=/d' -e '/set "PRIVATE_ROOT=/d' -e '/^@echo off/d'
  printf 'echo ROOT=%%ROOT%%\r\necho ENV=%%UV_PROJECT_ENVIRONMENT%%\r\n'
} > "$T/t.bat"
# no stdin any more: nothing is asked, the argument decides
run() { cmd //c "$W\t.bat" "$@" 2>&1 | tr -d '\r' | grep -oE '(ROOT|ENV)=[^ ]+' | tr '\n' ' '; }
ck() { [[ $2 == *"$3"* ]] || { echo "FAIL $1: $2"; exit 1; }; echo "ok  $1"; }

mkdir -p "$T/private" "$T/shared"; touch "$T/private/uv.exe" "$T/shared/uv.exe"

# an existing private install wins over a shared one
ck private-wins "$(run)" "ROOT=$W\private "

# ... but asking for private explicitly wins over an existing shared install
rm -rf "$T/private"
ck arg-beats-shared "$(run private)" "ROOT=$W\private "

# shared root only -> our own env folder inside it
ck shared "$(run)" 'shared\envs\sabg_analyzer '

# nothing installed yet: a plain double-click takes shared, no question asked
rm -rf "$T/shared"
ck fresh-default "$(run)"         "ROOT=$W\shared "
ck fresh-private "$(run private)" "ROOT=$W\private "
ck fresh-PRIVATE "$(run PRIVATE)" "ROOT=$W\private "
ck fresh-2       "$(run 2)"       "ROOT=$W\private "
echo "root detection OK"
