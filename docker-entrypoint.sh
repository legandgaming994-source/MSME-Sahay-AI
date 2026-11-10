#!/bin/sh
# Container entrypoint.
#
# Persistent disks on some platforms (notably Render) are mounted root-owned, so an unprivileged process cannot write
# to them. When started as root this script prepares the configured data directories (create, correct ownership), then
# DROPS PRIVILEGES and execs the command, so the application itself still runs as the unprivileged user and, as PID 1,
# receives SIGTERM directly (graceful shutdown). When already unprivileged (docker run --user, most orchestrators) it
# simply execs the command.
set -eu

APP_USER="${APP_USER:-app}"

prepare_dir() {
    dir="$1"
    [ -n "$dir" ] || return 0
    case "$dir" in
        /*) ;;
        *) echo "entrypoint: refusing non-absolute data directory '$dir'" >&2; exit 64 ;;
    esac
    case "$dir" in
        */../*|*/..) echo "entrypoint: refusing data directory containing '..': '$dir'" >&2; exit 64 ;;
    esac
    mkdir -p "$dir"
    chown "$APP_USER" "$dir"
    chmod 0700 "$dir"
    # repair only entries that are not already owned by the app user (cheap on restarts, no full recursive chown)
    find "$dir" ! -user "$APP_USER" -exec chown "$APP_USER" {} + 2>/dev/null || true
}

if [ "$(id -u)" = "0" ]; then
    prepare_dir "${MSME_STORAGE_DIR:-}"
    prepare_dir "${LAMBDA_ERP_UPLOAD_DIR:-}"
    case "${LAMBDA_ERP_DB:-}" in
        /*) prepare_dir "$(dirname "$LAMBDA_ERP_DB")" ;;     # a SQLite file path; a postgres URL is not a directory
    esac
    exec setpriv --reuid="$APP_USER" --regid="$(id -g "$APP_USER")" --init-groups -- "$@"
fi

exec "$@"
