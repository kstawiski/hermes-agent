#!/usr/bin/env bash
# Canonical test runner for hermes-agent. Run this instead of calling
# `pytest` directly to guarantee your local run matches CI behavior.
#
# What this script enforces:
#   * Per-file isolation via scripts/run_tests_parallel.py — each test
#     file runs in its own freshly-spawned `python -m pytest <file>`
#     subprocess. No xdist, no shared workers, no module-level leakage
#     between files.
#   * TZ=UTC, LANG=C.UTF-8, PYTHONHASHSEED=0 (deterministic)
#   * Env vars blanked (conftest.py also does this, but this
#     is belt-and-suspenders for anyone running pytest outside our
#     conftest path — e.g. on a single file)
#   * Proper venv activation (probes .venv, venv, then ~/.hermes/...)
#
# Usage:
#   scripts/run_tests.sh                            # full suite
#   scripts/run_tests.sh -j 4                       # cap parallelism
#   scripts/run_tests.sh tests/agent/               # discover only here
#   scripts/run_tests.sh tests/agent/ tests/acp/    # multiple roots
#   scripts/run_tests.sh tests/foo.py               # single file
#   scripts/run_tests.sh tests/foo.py -q            # path + bare pytest flag
#   scripts/run_tests.sh tests/foo.py -v --tb=long  # bare flags "just work"
#   scripts/run_tests.sh -k 'pattern'               # value flags pass through too
#   scripts/run_tests.sh tests/foo.py -- --tb=long  # explicit '--' still works
#
# Bare pytest flags (anything starting with '-' that isn't one of this
# runner's own options: -j/--jobs, --paths, --slice, --file-timeout, etc.)
# are forwarded to each per-file pytest invocation automatically — no '--'
# separator required. The explicit '--' form still works and stacks with
# bare flags. Positional path arguments override the default discovery
# root (tests/).

set -euo pipefail

# ── Locate repo root ────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# ── Locate python ───────────────────────────────────────────────────────────
# Honour an explicit dev interpreter first, then probe local venvs.
# HERMES_PYTHON is exported by the Nix devShell hook and ships [dev] extras.
#
# A candidate must have the default test dependencies, not merely pytest.
# A production venv can include pytest while omitting pytest-asyncio; selecting
# it makes every async test fail only after the expensive suite has started.
PYTHON=""
SKIPPED_VENVS=""

candidate_has_test_runtime() {
  "$1" -c 'import pytest, pytest_asyncio, acp, defusedxml' 2>/dev/null
}

if [ -n "${HERMES_PYTHON:-}" ] && [ -x "$HERMES_PYTHON" ]; then
  if candidate_has_test_runtime "$HERMES_PYTHON"; then
    PYTHON="$HERMES_PYTHON"
    echo "▶ using explicit dev venv via HERMES_PYTHON: $PYTHON"
  else
    SKIPPED_VENVS="$SKIPPED_VENVS $HERMES_PYTHON"
  fi
fi

for candidate in "$REPO_ROOT/.venv" "$REPO_ROOT/venv" "$HOME/.hermes/hermes-agent/venv"; do
  [ -z "$PYTHON" ] || break
  if [ -f "$candidate/bin/activate" ]; then
    if candidate_has_test_runtime "$candidate/bin/python"; then
      PYTHON="$candidate/bin/python"
      break
    fi
    SKIPPED_VENVS="$SKIPPED_VENVS $candidate"
  fi
done

if [ -n "$SKIPPED_VENVS" ]; then
  for skipped in $SKIPPED_VENVS; do
    echo "▶ skipping venv without default test dependencies (pytest, pytest_asyncio, acp, defusedxml): $skipped" >&2
  done
fi

if [ -z "$PYTHON" ]; then
  echo "error: no virtualenv with default test dependencies found in $REPO_ROOT/.venv or $REPO_ROOT/venv," >&2
  echo "       and HERMES_PYTHON is not a python with pytest+pytest_asyncio+acp+defusedxml (install .[dev], enter the Nix devShell, or create a venv)" >&2
  if [ -n "$SKIPPED_VENVS" ]; then
    echo "       (skipped for missing default test dependencies:$SKIPPED_VENVS — install dev extras there, or create $REPO_ROOT/.venv)" >&2
  fi
  exit 1
fi


# ── Live-gateway plugin (computed before we drop env) ───────────────────────
EXTRA_PYTHONPATH=""
EXTRA_PYTEST_PLUGINS=""
if [ -f "$HOME/.hermes/pytest_live_guard.py" ]; then
  EXTRA_PYTHONPATH="$HOME/.hermes"
  EXTRA_PYTEST_PLUGINS="pytest_live_guard"
fi


# ── Run in hermetic env ──────────────────────────────────────────────────────
# env -i: start with empty environment, opt-in only what we need.
# No credential var can leak — you'd have to explicitly add it here.
echo "▶ running per-file parallel test suite via run_tests_parallel.py"
echo "  (TZ=UTC LANG=C.UTF-8 PYTHONHASHSEED=0; clean env)"

cd "$REPO_ROOT"

# ── Pre-compile .pyc bytecode cache ─────────────────────────────────────────
# Each test file runs in its own subprocess via run_tests_parallel.py.
# Pre-building the bytecode cache once here (instead of each subprocess
# compiling on first import) avoids redundant work across ~2000 processes.
# Uses git to list tracked .py files (skips venv, node_modules, etc).
echo "▶ pre-compiling bytecode cache"
TRACKED_PYTHON=()
mapfile -d '' -t TRACKED_PYTHON < <(git ls-files -z '*.py')
if [ "${#TRACKED_PYTHON[@]}" -gt 0 ]; then
  "$PYTHON" -m compileall -q -j 0 -- "${TRACKED_PYTHON[@]}" >/dev/null 2>&1 || true
fi

echo "▶ launching test runner"
exec env -i \
  PATH="$PATH" \
  HOME="$HOME" \
  TZ=UTC \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  PYTHONHASHSEED=0 \
  ${HERMES_RUN_SLOW_PET_TESTS:+HERMES_RUN_SLOW_PET_TESTS="$HERMES_RUN_SLOW_PET_TESTS"} \
  ${HERMES_TEST_WORKERS:+HERMES_TEST_WORKERS="$HERMES_TEST_WORKERS"} \
  ${HERMES_TEST_PATHS:+HERMES_TEST_PATHS="$HERMES_TEST_PATHS"} \
  ${HERMES_TEST_FILE_TIMEOUT:+HERMES_TEST_FILE_TIMEOUT="$HERMES_TEST_FILE_TIMEOUT"} \
  ${HERMES_TEST_FILE_RETRIES:+HERMES_TEST_FILE_RETRIES="$HERMES_TEST_FILE_RETRIES"} \
  ${HERMES_TEST_SLICE:+HERMES_TEST_SLICE="$HERMES_TEST_SLICE"} \
  ${EXTRA_PYTHONPATH:+PYTHONPATH="$EXTRA_PYTHONPATH"} \
  ${EXTRA_PYTEST_PLUGINS:+PYTEST_PLUGINS="$EXTRA_PYTEST_PLUGINS"} \
  "$PYTHON" "$SCRIPT_DIR/run_tests_parallel.py" "$@"
