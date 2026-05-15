#!/usr/bin/env bash
# scripts/publish-pending.sh PHASE
#
# Publishes the wheels under dist-pending/ corresponding to PHASE (1 or 2).
# Designed to be both manually-invokable and idempotent — re-running after
# a partial success will just have PyPI reject the already-uploaded
# versions with HTTP 400 (a no-op error, not a real failure).
#
# Spreading across phases is required because PyPI throttles new-project
# creation per user (~2/day). Existing-project version bumps don't count
# toward that quota.
#
# Phase 1 (1 version bump + 2 new projects, AT quota limit):
#   - khimaira v0.1.2          (existing project, version bump)
#   - khimaira-chat v0.1.0     (NEW project)
#   - khimaira-scarlet v0.2.0  (NEW project)
#
# Phase 2 (2 new projects, AT quota limit):
#   - khimaira-seance v0.2.0   (NEW project)
#   - khimaira-sibyl v0.2.0    (NEW project)
#
# Auth: reads PYPI_API_TOKEN from env (set in ~/.bashrc).
# Pacing: 30s between each upload to be polite.

set -u
PHASE="${1:-}"
if [[ "$PHASE" != "1" && "$PHASE" != "2" ]]; then
  echo "Usage: $0 <1|2>" >&2
  exit 2
fi

if [[ -z "${PYPI_API_TOKEN:-}" ]]; then
  echo "ERROR: PYPI_API_TOKEN not set in env. Source ~/.bashrc first." >&2
  exit 3
fi

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="$REPO_ROOT/dist-pending"

if [[ ! -d "$DIST" ]]; then
  echo "ERROR: $DIST does not exist. Run 'uv build' for each package first." >&2
  exit 4
fi

if [[ "$PHASE" == "1" ]]; then
  PACKAGES=(khimaira khimaira_chat khimaira_scarlet)
else
  PACKAGES=(khimaira_seance khimaira_sibyl)
fi

echo "=== publish-pending.sh phase $PHASE ==="
echo "packages: ${PACKAGES[*]}"
echo ""

OK_COUNT=0
FAIL_COUNT=0
ALREADY_COUNT=0
declare -a RESULTS=()

for pkg in "${PACKAGES[@]}"; do
  WHEELS=("$DIST/${pkg}"-*.whl)
  if [[ ! -f "${WHEELS[0]}" ]]; then
    echo "SKIP $pkg — no wheel found in $DIST" >&2
    RESULTS+=("SKIP $pkg (no wheel)")
    continue
  fi
  WHL="${WHEELS[0]}"
  TAR="${WHL%-*-*-*.whl}.tar.gz"
  # Find the matching tar.gz (uv naming convention varies slightly per pkg)
  TARS=("$DIST/${pkg}"-*.tar.gz)
  if [[ -f "${TARS[0]}" ]]; then
    TAR="${TARS[0]}"
  fi

  echo "=== publishing $pkg ==="
  echo "    wheel: $(basename "$WHL")"
  echo "    sdist: $(basename "$TAR")"

  OUTPUT=$(UV_PUBLISH_TOKEN="$PYPI_API_TOKEN" uv publish "$TAR" "$WHL" 2>&1)
  RC=$?
  echo "$OUTPUT" | tail -10

  if [[ $RC -eq 0 ]]; then
    OK_COUNT=$((OK_COUNT + 1))
    RESULTS+=("OK   $pkg")
  elif echo "$OUTPUT" | grep -qiE "already exists|file name has been used|400"; then
    # Idempotent re-run: PyPI rejected because the version already lives.
    ALREADY_COUNT=$((ALREADY_COUNT + 1))
    RESULTS+=("DUP  $pkg (already on PyPI — no-op)")
  else
    FAIL_COUNT=$((FAIL_COUNT + 1))
    RESULTS+=("FAIL $pkg")
  fi

  echo "--- sleeping 30s before next ---"
  sleep 30
done

echo ""
echo "=== summary ==="
for r in "${RESULTS[@]}"; do echo "  $r"; done
echo ""
echo "ok=$OK_COUNT  duplicate=$ALREADY_COUNT  fail=$FAIL_COUNT"

if [[ $FAIL_COUNT -gt 0 ]]; then
  exit 1
fi
exit 0
