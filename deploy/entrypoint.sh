#!/bin/sh
# Container entrypoint: prepare the data volume, drop root, optionally start the
# survey worker next to the API (they must share one SQLite file and one disk).
set -eu

if [ "$(id -u)" = "0" ]; then
    # Platform volumes (Render disks) are mounted root-owned.
    mkdir -p "$PLANETX_CACHE_DIR" "$(dirname "$PLANETX_DB")"
    chown app:app "$PLANETX_CACHE_DIR" "$(dirname "$PLANETX_DB")"
    exec setpriv --reuid=app --regid=app --init-groups "$0" "$@"
fi

if [ "${PLANETX_RUN_WORKER:-0}" = "1" ]; then
    python -m planetx.survey init   # idempotent
    (
        # `run` returns when the queue drains or on an error; wait, then look again
        # (new data releases, requeued patches). The API is the foreground process.
        while true; do
            python -m planetx.survey run --processor motion || echo "survey worker exited with $?" >&2
            sleep 300
        done
    ) &
fi

exec "$@"
