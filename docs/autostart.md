# Running orgtree unattended (F-06 §9)

Boot-start is the easy half. The hard half is that `update.sh` DETACHES the
backend and exits — so a naive "restart on failure" watches a launcher that
already succeeded and never notices the backend die. The installer below runs
the backend under systemd instead.

## `tools/install-autostart.sh`

One systemd **user** unit running the backend **in the foreground**
(`Type=simple`, `Restart=always`, `RestartSec=10`). Under systemd there is no
stale-backend race to guard — systemd owns the real process — so direct launch
is correct, and both boot-start and crash-restart come from the same unit.

It is a **user** unit on purpose: the CLI reads `~/.claude/.credentials.json`
from the user's home, and a system-level service resolves a different `~` — the
org boots, the UI serves, and only the turns die (§9.1).

For a box with nobody logged in, also run once: `loginctl enable-linger $USER`.

Deploys under systemd: `git pull` + build the UI, then
`systemctl --user restart orgtree`. (Manual `update.sh` runs still work, but
not while the unit is active — two owners of one port.)

## The hub

`hub/compose.yaml` carries `restart: unless-stopped`, which gives the hub
start-on-boot once the Docker daemon itself starts with the machine
(`systemctl enable docker`).

## What must be true of the ORG, not just the process (§9.4)

- **`auto_resume` on** — forced automatically when headless is enabled;
  strongly recommended for any unattended org.
- **An API key** (settings → autonomy) removes the hard ceiling on unattended
  subscription auth: the OAuth **refresh token expires** (~15-day window
  measured), renewal is interactive, and the failure mode is every turn dying
  at an unpredictable hour. The credential watcher warns each org's inbox
  when expiry is near — but a key makes the whole class impossible, and
  **headless mode requires one** (hard rule, both directions).
- **Headless** (settings → autonomy): agents are told no user is present;
  questions/credit requests/user audiences auto-deny; mail to the user is
  stored with a "no reply is coming" note (the inbox is the audit trail);
  the overseer renders grey with an empty eye; fable policies must be
  non-halt before it enables.
