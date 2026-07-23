#!/bin/bash
set -e
# Azure File Share (SMB) doesn't support POSIX locks (flock/fcntl) needed by
# Chromium's SingletonLock. Copy the profile to local disk before launching.
if [ -d "/browser-profile" ] && [ -n "$(ls -A /browser-profile 2>/dev/null)" ]; then
    mkdir -p /tmp/browser-profile
    cp -ra /browser-profile/. /tmp/browser-profile/
fi
exec /app/.venv/bin/joinly "$@"
