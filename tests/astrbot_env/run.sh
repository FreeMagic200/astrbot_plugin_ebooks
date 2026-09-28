#!/bin/sh
# Regression suite for the ebooks plugin. The plugin only imports inside AstrBot,
# so the tests run in the AstrBot Docker container.
#
#   tests/astrbot_env/run.sh                       # test this checkout
#   tests/astrbot_env/run.sh /path/to/plugin_dir   # test another copy of the plugin
#   tests/astrbot_env/run.sh "" -k rerank          # extra args go to pytest
#   ASTRBOT_CONTAINER=name tests/astrbot_env/run.sh  # container name (default: astrbot)
#
# Tests marked @pytest.mark.fixed pin bugs fixed since 2026-09-23; everything else is a
# regression guard. Nothing here touches the network or the live plugin config.
set -e
HERE=$(cd "$(dirname "$0")" && pwd)
CONTAINER=${ASTRBOT_CONTAINER:-astrbot}
SRC=${1:-$HERE/../..}
[ $# -gt 0 ] && shift
[ -n "$SRC" ] || SRC="$HERE/../.."
SRC=$(cd "$SRC" && pwd)

STAGE=$(mktemp -d)
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/v/data/plugins"
touch "$STAGE/v/data/__init__.py" "$STAGE/v/data/plugins/__init__.py"
rsync -a --exclude __pycache__ --exclude .ruff_cache --exclude .git --exclude /tests \
  "$SRC/" "$STAGE/v/data/plugins/astrbot_plugin_ebooks/"

docker exec "$CONTAINER" rm -rf /tmp/ebtest
docker exec "$CONTAINER" mkdir -p /tmp/ebtest
docker cp "$STAGE/v" "$CONTAINER":/tmp/ebtest/v
docker cp "$HERE" "$CONTAINER":/tmp/ebtest/tests

status=0
docker exec -w /AstrBot -e EBOOKS_ROOT=/tmp/ebtest/v -e PYTHONDONTWRITEBYTECODE=1 "$CONTAINER" \
  python -m pytest /tmp/ebtest/tests -p no:cacheprovider -p no:warnings -q "$@" || status=$?
docker exec "$CONTAINER" rm -rf /tmp/ebtest
exit $status
