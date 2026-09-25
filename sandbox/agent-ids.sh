#!/bin/sh
# Usage: orgtree-agent-ids UID GID
#
# Give the `agent` user the host backend's uid and gid. Native Linux Docker
# does not translate ownership on bind mounts, so the agent and the backend
# can both write home, workspace and scratch only when their uids match.
#
# Runs at image build, and again in a throwaway root container over the org's
# /etc volume before every container create: that volume is seeded from the
# image once, so an older org keeps whatever `agent` it first had.
# Idempotent; a no-op when `agent` already has UID and GID.
set -eu
uid=$1
gid=$2

if [ "$(id -u agent 2>/dev/null)" = "$uid" ] \
        && [ "$(id -g agent 2>/dev/null)" = "$gid" ]; then
    exit 0
fi

# another user holding the uid would make `agent` a second name for it, and
# sudo resolves the uid to the first name (node:22-slim's `node` holds 1000)
holder=$(getent passwd "$uid" | cut -d: -f1)
if [ -n "$holder" ] && [ "$holder" != agent ]; then
    userdel "$holder"
fi

# an existing group with the gid (e.g. `users`) is reused rather than removed
if ! getent group "$gid" >/dev/null; then
    if getent group agent >/dev/null; then
        groupmod -g "$gid" agent
    else
        groupadd -g "$gid" agent
    fi
fi

if id agent >/dev/null 2>&1; then
    usermod -u "$uid" -g "$gid" agent
else
    useradd -m -s /bin/bash -u "$uid" -g "$gid" agent
fi
