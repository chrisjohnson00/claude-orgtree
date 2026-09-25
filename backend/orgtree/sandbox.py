# pyright: strict
"""Docker sandboxes for kiosk orgs (user spec).

Every kiosk org created with `sandbox: true` runs its agents' turns inside ONE
dedicated container (image: sandbox/Dockerfile) — genuine terminal use with no
view of the host: no host filesystem, no host processes, per-container CPU and
memory caps. Non-kiosk orgs are untouched and run natively.

Container layout (bind mounts):
    /home/agent                          <data>/sandboxes/<slug>/home   (persists
                                         ~/.claude — session transcripts — so
                                         resume-on-demand and read_chat work)
    /home/agent/orgtree/workspaces/<slug>   the org workspace (the ONE window)
    /home/agent/orgtree/scratch/<slug>      node scratch dirs (turn cwds)
    /opt/orgtree-backend                 this backend, read-only (mcptool.py +
                                         steer.py run in-container via python3)

The in-container data layout mirrors the host's ~/orgtree shape on purpose:
steer.py's cwd-derived identity and its ~/orgtree fallback work unchanged.
`.bridge` in the container's data root carries the backend URL
(host.docker.internal:<bridge port>) and the org's sandbox secret — the only
door out, gated by api.BridgeGateway.

Auth: the default is the PROXIED SUBSCRIPTION — the container's CLI points at
the bridge's /anthropic/<secret> proxy (host-side OAuth, no credential file
ever enters the sandbox). A kiosk `api_key` (creation form / dashboard) or
ORGTREE_SANDBOX_API_KEY overrides it with a plain env key, and the literal
value 'subscription' copies host credentials into the sandbox home.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
from typing import TYPE_CHECKING, Any

from . import store

if TYPE_CHECKING:
    from .ledger import Org

_DATA: str = os.path.expanduser(os.environ.get("ORGTREE_DATA", "~/orgtree"))
REPO_ROOT: str = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
BACKEND_DIR: str = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))

IMAGE: str = os.environ.get("ORGTREE_SANDBOX_IMAGE", "orgtree-sandbox")
# bump when sandbox/Dockerfile changes: the tag carries the revision, so
# existing images rebuild and running containers recreate on their next turn
# (r2: passwordless sudo — agents hold root inside the container)
IMG_REV: str = "r2"
BRIDGE_PORT: int = int(os.environ.get("ORGTREE_BRIDGE_PORT", "7362") or 0)
MEM: str = os.environ.get("ORGTREE_SANDBOX_MEM", "4g")
CPUS: str = os.environ.get("ORGTREE_SANDBOX_CPUS", "2")

# Storage: the rootfs runs READ-ONLY; /tmp and /run are sized RAM tmpfs,
# bounded by --memory. System dirs are per-org named volumes; home, workspace
# and scratch are host bind mounts. There is no per-org storage cap.
SYS_DIRS: tuple[str, ...] = ("usr", "var", "etc", "opt", "root", "srv")
TMP_SIZE: str = os.environ.get("ORGTREE_SANDBOX_TMP", "1g")
RUN_SIZE: str = os.environ.get("ORGTREE_SANDBOX_RUN", "64m")


def sys_volume(slug: str, d: str) -> str:
    return f"orgtree-sys-{slug}-{d}"


# container layout generation — a label on the container; mismatch = recreate
LAYOUT: str = "binds-v1"


def usrlocal_volume(ver: str) -> str:
    """The version-tagged READ-ONLY /usr/local (user verdict: the CLI stays
    image-pinned even though /usr is a writable volume). Docker seeds it from
    the image on first mount; the version in the name is the №44 invariant —
    a host CLI update moves the name, and the fresh volume seeds from the
    freshly built image."""
    return f"orgtree-usrlocal-{ver}-{IMG_REV}"


_build_lock: threading.Lock = threading.Lock()


def _cfg(org: Org) -> dict[str, str] | None:
    """Sandbox config for ANY org (user ruling: not just kiosks): kiosks
    carry it inside their kiosk dict; normal orgs in a top-level `sandbox`."""
    k = org.d.get("kiosk") or {}
    if k.get("sandbox"):
        return {"secret": k.get("sandbox_secret", "")}
    s = org.d.get("sandbox") or {}
    if s.get("enabled"):
        return {"secret": s.get("secret", "")}
    return None


def is_sandboxed(org: Org) -> bool:
    return _cfg(org) is not None


_docker_ok: bool | None = None


def docker_available() -> bool:
    """Is a docker CLI on PATH? (cached — the UI disables the sandbox
    checkbox entirely when it isn't; user ruling)"""
    global _docker_ok
    if _docker_ok is None:
        _docker_ok = shutil.which("docker") is not None
    return _docker_ok


def sandbox_secret(org: Org) -> str:
    """The persisted org-wide bridge secret ("" for an unsandboxed org)."""
    kiosk = org.d.get("kiosk") or {}
    if kiosk.get("sandbox"):
        return str(kiosk.get("sandbox_secret") or "")
    sandbox = org.d.get("sandbox") or {}
    if sandbox.get("enabled"):
        return str(sandbox.get("secret") or "")
    return ""


def uses_subscription_auth(k: dict[str, Any] | None) -> bool:
    """True when the org's sandbox would run on COPIED host credentials
    (the 'subscription' escape hatch — docstring: private single-user
    installs only). Security review 2026-08-01: this mode and a PUBLIC
    kiosk URL are mutually exclusive STRUCTURALLY — the OAuth token lands
    in the sandbox home and root-in-container can copy it to any path the
    kiosk exposes; no filename denylist can be a boundary. Both
    enable-orderings are refused."""
    key = ((k or {}).get("api_key")
           or os.environ.get("ORGTREE_SANDBOX_API_KEY") or "proxied")
    return str(key).strip().lower() == "subscription"


def container_name(slug: str) -> str:
    return "orgtree-" + slug


def sandbox_root(slug: str) -> str:
    return os.path.join(_DATA, "sandboxes", slug)


def auth_label(org: Org, k: Any = None) -> str:
    """The container's auth as an identity token: `proxied`, `subscription`,
    or `key:<8 hex>` — a digest, never the key itself (labels are readable by
    anyone who can run `docker inspect`). Compared on every `ensure_container`
    so a settings change, a key rotation or an unset `ORGTREE_SANDBOX_API_KEY`
    recreates the container instead of leaving it billing the old way."""
    import hashlib
    auth = container_auth(org, k)
    low = auth.lower()
    if "prox" in low or low == "subscription":
        return low
    return "key:" + hashlib.sha256(auth.encode()).hexdigest()[:8]


def container_auth(org: Org, k: Any = None) -> str:
    """What a sandboxed org's container authenticates WITH: a literal API key,
    `"subscription"` (host credentials copied in), or `"proxied"` (the bridge
    attaches the host token per request).

    Factored out of `ensure_container` (redteam 2026-08-18) because a second
    caller needs the same answer and a hand-mirrored copy would drift:
    `supervisor.bills_the_key` has to know whether a limit error came off the
    org's own key or the host subscription, and reading `org.d["api_key"]`
    alone missed BOTH the kiosk-level key and `ORGTREE_SANDBOX_API_KEY` — a
    per-minute API rate limit was then timed off the subscription's lanes."""
    k = (org.d.get("kiosk") or {}) if k is None else k
    return (("" if org.d.get("api_fallback")
             else str(org.d.get("api_key") or ""))
            or str(k.get("api_key") or "")
            or os.environ.get("ORGTREE_SANDBOX_API_KEY")
            or "proxied").strip()


def shared_container_auth_env(org: Org, k: Any = None) -> dict[str, str]:
    """Auth env baked into the shared container at ``docker run``."""
    key = container_auth(org, k)
    low = key.lower()
    if "prox" in low:
        return {
            "ANTHROPIC_BASE_URL": f"{bridge_url()}/anthropic/{sandbox_secret(org)}",
            "ANTHROPIC_API_KEY": "orgtree-proxied",
        }
    if low == "subscription":
        return {}
    return {"ANTHROPIC_API_KEY": key}


def anthropic_proxy_api_key(org: Org, *, fallback_active: bool = False) -> str:
    """Explicit key the host-side Anthropic relay should attach.

    Literal keys live directly in the container, so the relay only ever
    carries proxied traffic; api-fallback temporarily switches that traffic
    to the org key. An empty return means the relay should use the host OAuth
    subscription.
    """
    if fallback_active:
        return str(org.d.get("api_key") or "").strip()
    return ""


def sandbox_home(slug: str) -> str:
    return os.path.join(sandbox_root(slug), "home")


# in-container paths (the mirror of the host layout)
def cpath_data() -> str:
    return "/home/agent/orgtree"


def cpath_workspace(slug: str) -> str:
    return f"{cpath_data()}/workspaces/{slug}"


def cpath_scratch(slug: str, nid: str) -> str:
    return f"{cpath_data()}/scratch/{slug}/{nid.split('@')[0]}"


def bridge_url() -> str:
    """The bridge address visible inside an agent container."""
    return f"http://host.docker.internal:{BRIDGE_PORT}"


def bridge_file_config(org: Org) -> dict[str, str]:
    """Shared ``.bridge`` content: the bridge URL and the org's secret."""
    return {"url": bridge_url(), "secret": sandbox_secret(org)}


def chown_agent(org: Org, nid: str, *rel: str) -> None:
    """Hand a backend-minted path inside a sandboxed org to the agent.

    The backend writes through the host bind mounts as its own uid, which
    the container does not map to `agent` (uid 1001) — the CLI runs as
    `agent`. A root-owned `outbox/` or `uploads/` reads to the
    agent as "my scratch is broken" (live bug 2026-08-04, kiosk `vnuser`).
    Best-effort by design: with the container down the exec fails silently,
    and the start-time heal in ensure_container covers it instead."""
    if not is_sandboxed(org):
        return
    slug = org.d["slug"]
    target = "/".join((cpath_scratch(slug, nid), *rel))
    try:
        # ⚠ -u root: exec inherits the image's USER agent, and an unprivileged
        # chown fails "Operation not permitted" — silently, given the swallow
        # below (caught live 2026-08-05 healing vnuser by hand)
        _docker("exec", "-u", "root", container_name(slug),
                "chown", "-R", "agent:agent", target, timeout=30)
    except Exception:                                        # noqa: BLE001
        pass


def chown_home_path(org: Org, host_path: str) -> None:
    """`chown_agent` for a path under the container HOME rather than the data
    root — the transcript store (`/home/agent/.claude/projects/…`) is the case
    that needed it (2026-08-20).

    `chown_agent` only builds paths under `cpath_scratch`, and `_heal_ownership`
    only sweeps `cpath_data()` = /home/agent/orgtree, so nothing covered
    ~/.claude at all. A session file the backend mints there — the cut that
    turns a CLI-compacted generation into a consultable bearer — lands
    root-owned, and the agent that rehires it can read but not append, so the
    bearer fails on the first write of its resumed turn.

    Best-effort on the same terms as chown_agent: a host path outside this
    org's sandbox home, or a container that is down, is a silent no-op."""
    if not is_sandboxed(org):
        return
    slug = org.d["slug"]
    try:
        rel = os.path.relpath(host_path, sandbox_home(slug))
    except ValueError:              # different drive — not ours to touch
        return
    if rel.startswith(".."):        # outside the container home
        return
    target = "/home/agent/" + rel.replace("\\", "/")
    try:
        _docker("exec", "-u", "root", container_name(slug),
                "chown", "agent:agent", target, timeout=30)
    except Exception:                                        # noqa: BLE001
        pass


def _heal_ownership(name: str) -> None:
    """Every path the backend minted while the container was DOWN is
    root-owned (see chown_agent) — hand the whole data tree back to the agent
    at container start. Also fixes Docker's own root-owned mount scaffolding
    (/home/agent/orgtree, …/scratch, …/workspaces), which the agent sees when
    it looks one level above its own folder."""
    _docker("exec", "-u", "root", name, "chown", "-R", "agent:agent",
            cpath_data(), timeout=120)


def _docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          timeout=timeout)


def docker_ok() -> bool:
    try:
        return _docker("version", "--format", "{{.Server.Version}}").returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _desired_image_tag() -> str:
    """Return the image tag to run without building or pulling it."""
    from . import supervisor        # lazy — supervisor imports this module
    ver = supervisor.cli_version()
    return f"{IMAGE}:{ver}-{IMG_REV}" if ver != "unknown" else IMAGE


def ensure_image() -> str:
    """№44 (user-approved): the image is TAGGED with the host CLI's version
    and pins the same version inside — when the host CLI updates, the next
    sandboxed turn rebuilds instead of running a CLI frozen at first-build.
    Returns the tag to run."""
    from . import supervisor        # lazy — supervisor imports this module
    ver = supervisor.cli_version()
    tag = _desired_image_tag()
    if _docker("image", "inspect", tag).returncode == 0:
        return tag
    with _build_lock:
        if _docker("image", "inspect", tag).returncode == 0:
            return tag
        args = ["build", "-t", tag]
        if ver != "unknown":
            args += ["--build-arg", f"CLAUDE_VERSION={ver}"]
        r = _docker(*args, os.path.join(REPO_ROOT, "sandbox"), timeout=1200)
        if r.returncode != 0:
            raise RuntimeError("sandbox image build failed: "
                               + (r.stderr or r.stdout)[-500:])
    return tag


def ensure_container(org: Org) -> str:
    """The org's container, created on first need and restarted if stopped.
    Raises RuntimeError with an actionable message when it cannot run."""
    slug = org.d["slug"]
    k = org.d.get("kiosk") or {}
    name = container_name(slug)
    if not docker_ok():
        raise RuntimeError("Docker is not running — start Docker Desktop "
                           "(kiosk sandboxes run their turns in containers)")
    from . import supervisor        # lazy — supervisor imports this module
    want = _desired_image_tag()
    ins = _docker("container", "inspect", "-f",
                  "{{.State.Running}} {{.Config.Image}} "
                  '{{index .Config.Labels "orgtree.layout"}} '
                  '{{index .Config.Labels "orgtree.auth"}}', name)
    if ins.returncode == 0:
        parts = ins.stdout.split()
        running = parts[0] if parts else ""
        cur_img = parts[1] if len(parts) > 1 else ""
        layout = parts[2] if len(parts) > 2 else ""
        cur_auth = parts[3] if len(parts) > 3 else ""
        # №44: the CLI rides the version-tagged read-only /usr/local volume,
        # so an image move requires a recreate (which re-mounts that volume);
        # an older mount layout recreates too.
        # AUTH likewise (redteam 2026-08-18): `docker run -e
        # ANTHROPIC_API_KEY=…` bakes the credential in, so a container created
        # under one auth kept billing that way after the org's settings moved
        # — and `supervisor.bills_the_key`, which reads the CONFIG, then timed
        # the container's own API limits against the host subscription's
        # lanes. Recreating on change is what makes the config truthful.
        if (cur_img and cur_img != want) or layout != LAYOUT \
                or cur_auth != auth_label(org, k):
            _docker("rm", "-f", name, timeout=60)
        else:
            if running != "true":
                _docker("start", name)
                _heal_ownership(name)
            return name
    # auth (user ruling): PROXIED SUBSCRIPTION is the default for every kiosk
    # — the container's CLI talks to the bridge's /anthropic passthrough and
    # the HOST attaches the OAuth token; no credential ever enters the
    # sandbox. ORGTREE_SANDBOX_API_KEY remains a hidden escape hatch (a real
    # API key, or 'subscription' to copy the host credentials in).
    # §9.5: the ORG-LEVEL key (settings, any org — promoted out of the kiosk
    # spec) outranks the kiosk field; proxy mode and key mode stay mutually
    # exclusive — a set key wins and the bridge proxy is not used.
    # api_fallback (2026-08-17): a fallback org must stay PROXIED — container
    # env is fixed at `docker run`, so the per-request auth flip lives in the
    # bridge's /anthropic passthrough instead; skipping the org key here is
    # what routes it there
    key = container_auth(org, k)
    use_sub = key.lower() == "subscription"
    if use_sub and k.get("enabled") and k.get("token"):
        # structural, not a filter: see uses_subscription_auth
        raise RuntimeError(
            "subscription-auth sandbox with a PUBLIC kiosk URL is refused — "
            "the copied host credentials live in the sandbox home, readable "
            "by root in the container. Disable the kiosk URL or switch to proxied auth.")
    image_tag = ensure_image()
    home = sandbox_home(slug)
    os.makedirs(os.path.join(home, "orgtree"), exist_ok=True)
    if use_sub:
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        src = os.path.expanduser("~/.claude/.credentials.json")
        if not os.path.isfile(src):
            raise RuntimeError("subscription mode: no Claude credentials found "
                               "at ~/.claude/.credentials.json")
        import shutil as _sh
        _sh.copy2(src, os.path.join(home, ".claude", ".credentials.json"))
        cfg = os.path.join(home, ".claude.json")
        if not os.path.exists(cfg):
            with open(cfg, "w", encoding="utf-8") as f:
                json.dump({"hasCompletedOnboarding": True}, f)
    ws = org.d.get("workspace")
    if not ws:
        # pre-workspace-era org doc: without this, makedirs TypeErrors (or a
        # literal "None" would be bind-mounted) — say what's wrong instead
        raise RuntimeError(f"org {slug!r} has no workspace directory recorded "
                           f"— cannot mount its sandbox container")
    os.makedirs(ws, exist_ok=True)
    scratch = store.scratch_root(slug)
    os.makedirs(scratch, exist_ok=True)
    bridge_doc = bridge_file_config(org)
    with open(os.path.join(home, "orgtree", ".bridge"), "w",
              encoding="utf-8") as f:
        json.dump(bridge_doc, f)
    r = _docker(
        "run", "-d", "--name", name,
        "--label", f"orgtree.layout={LAYOUT}",
        "--label", f"orgtree.auth={auth_label(org, k)}",
        "--memory", MEM, "--cpus", CPUS,
        # rootfs read-only: persistent writes land only in the system-dir
        # volumes or the host binds below. /usr/local is the version-tagged
        # READ-ONLY volume: the CLI stays image-pinned under the /usr shadow.
        "--read-only",
        "--tmpfs", f"/tmp:rw,size={TMP_SIZE},mode=1777",
        "--tmpfs", f"/run:rw,size={RUN_SIZE}",
        *[a for d in SYS_DIRS for a in ("-v", f"{sys_volume(slug, d)}:/{d}")],
        "-v", f"{usrlocal_volume(supervisor.cli_version())}:/usr/local:ro",
        "--add-host", "host.docker.internal:host-gateway",
        *[item for env_key, env_val in
          sorted(shared_container_auth_env(org, k).items())
          for item in ("-e", f"{env_key}={env_val}")],
        "-v", f"{home}:/home/agent",
        "-v", f"{ws}:{cpath_workspace(slug)}",
        "-v", f"{scratch}:{cpath_data()}/scratch/{slug}",
        "-v", f"{BACKEND_DIR}:/opt/orgtree-backend:ro",
        image_tag, "sleep", "infinity", timeout=300)
    if r.returncode != 0:
        raise RuntimeError("sandbox container failed to start: "
                           + (r.stderr or r.stdout)[-500:])
    _heal_ownership(name)
    return name


def exec_argv(name: str, cwd: str,
              env: dict[str, str] | None = None) -> list[str]:
    """Prefix that runs a command inside the org's container."""
    args = ["docker", "exec", "-i", "-w", cwd]
    for key, value in sorted((env or {}).items()):
        args += ["-e", f"{key}={value}"]
    return args + [name]


def kill_claude(name: str, match: str = "claude") -> None:
    """Timeout hammer: killing the `docker exec` client on the host leaves the
    in-container process alive — reap it explicitly. `match` narrows the kill
    to one turn's process (its session id appears in the argv); the container
    is shared org-wide, so a blanket "claude" match would SIGKILL every other
    agent's turn too (gap audit №40)."""
    try:
        _docker("exec", name, "pkill", "-9", "-f", match, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        pass


_dead: set[str] = set()   # slugs deleted mid-warm-up (create/delete race)


def remove(slug: str) -> None:
    """Org deleted: tear the container AND its system volumes down (orphaned
    volumes are GBs nobody would reclaim; the org's FILES survive via the
    trash-rename of its doc + host dirs, but container system state follows
    the container). The tombstone closes the
    warm() race: deleting an org while its background prebuild was still
    creating the container used to leak it (rm -f fired before the container
    existed; observed in the wild)."""
    _dead.add(slug)
    try:
        _docker("rm", "-f", container_name(slug), timeout=60)
        _docker("volume", "rm", "-f", *[sys_volume(slug, d) for d in SYS_DIRS],
                timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        pass


def warm(org: Org) -> None:
    """Fire-and-forget prebuild at kiosk creation so the first turn is not
    minutes slow (image build + container create)."""
    slug = org.d["slug"]
    _dead.discard(slug)          # same-slug re-create un-tombs it

    def run() -> None:
        try:
            ensure_container(org)
        except Exception as e:              # noqa: BLE001 — surfaced per turn
            print(f"[orgtree] sandbox warm-up for {slug!r}: {e}")
        if slug in _dead:        # deleted while we were building — tear down
            try:
                _docker("rm", "-f", container_name(slug), timeout=60)
            except (OSError, subprocess.TimeoutExpired):
                pass
    threading.Thread(target=run, daemon=True).start()
