#!/bin/sh
# Runs briefly as root (the image's default USER), fixes up ownership of the
# bind-mounted data directory if needed, then drops to the unprivileged
# 'appuser' before exec'ing the real command. This container's /data is a
# host bind mount (./data:/data), not a fresh named volume — it may already
# exist on the host owned by root from before this image ran as non-root, so
# a plain Dockerfile `USER appuser` line alone is not enough: it would leave
# an existing root-owned host directory unwritable on the first deploy of
# this change. Fixing ownership here, at container start, handles both a
# fresh directory and a pre-existing root-owned one.
set -e

DATA_DIR="${DATA_DIR:-/data}"

if [ "$(id -u)" = "0" ]; then
    if [ -d "$DATA_DIR" ]; then
        owner="$(stat -c '%u' "$DATA_DIR")"
        if [ "$owner" != "$(id -u appuser)" ]; then
            echo "docker-entrypoint: chown $DATA_DIR to appuser (was uid $owner)"
            chown -R appuser:appuser "$DATA_DIR"
        fi
    fi
    exec setpriv --reuid=appuser --regid=appuser --clear-groups -- "$@"
fi

# Already non-root (e.g. someone set `user:` in compose) — just run it.
exec "$@"
