# The kiosk-org sandbox image (user spec): every kiosk org runs its agents'
# turns inside ONE dedicated container built from this image — real terminal
# use with no view of the host machine. The backend builds it on demand,
# tagged with the HOST CLI's version (№44): when the host CLI updates, the
# next sandbox turn rebuilds the image automatically instead of running a
# CLI frozen at first-build forever.
FROM node:22-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 git curl ca-certificates bash procps sudo \
    && rm -rf /var/lib/apt/lists/*

# the Claude Code CLI, pinned to the HOST's version so host and sandbox
# agents run the same binary (build-arg supplied by sandbox.ensure_image;
# "latest" only for manual builds)
ARG CLAUDE_VERSION=latest
RUN npm install -g @anthropic-ai/claude-code@${CLAUDE_VERSION}

# `agent` carries the host backend's uid/gid (build-args supplied by
# sandbox.ensure_image): bind mounts keep raw uids on native Linux, so any
# other uid leaves home, workspace or scratch writable from only one side.
# The script also re-runs over an org's existing /etc volume (see
# sandbox.ensure_container), so it ships in the image.
ARG AGENT_UID=1001
ARG AGENT_GID=1001
COPY agent-ids.sh /usr/local/sbin/orgtree-agent-ids
# agents get ROOT via passwordless sudo (user spec 2026-07-31): the container
# is the isolation boundary, so inside it they may install packages, edit
# system config, bind low ports — anything. The CLI itself keeps running as
# the non-root user (it dislikes running as root).
RUN orgtree-agent-ids "$AGENT_UID" "$AGENT_GID" \
    && echo 'agent ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/agent \
    && chmod 0440 /etc/sudoers.d/agent
USER agent
ENV HOME=/home/agent
WORKDIR /home/agent

# the container idles; turns arrive via `docker exec`
CMD ["sleep", "infinity"]
