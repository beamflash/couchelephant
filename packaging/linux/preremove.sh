#!/bin/sh
# Runs before the files go. An upgrade keeps the service; postinstall restarts it.
case "${1:-}" in
    remove|purge|0)
        if [ -d /run/systemd/system ] && command -v systemctl >/dev/null 2>&1; then
            systemctl stop couchelephant 2>/dev/null || true
            systemctl disable couchelephant 2>/dev/null || true
        fi
        ;;
esac
exit 0
