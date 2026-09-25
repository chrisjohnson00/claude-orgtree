"""Sandboxed orgs — the container execution mode, adversarially.

    .venv/Scripts/python.exe backend/tests/test_sandbox.py            hermetic
    .venv/Scripts/python.exe backend/tests/test_sandbox.py --docker   + real Docker

No pytest (it is not installed here). Plain asserts, `ok N` lines, one final
`ALL N CHECKS PASS`.

WHY THIS FILE EXISTS
--------------------
An entire execution mode was untested: `sandbox.ensure_container` had zero
references in any suite. This is also where the security ceiling lives: the
container is the boundary, and the bridge secret is the one key that opens
the door out of it.

THE TWO TIERS
-------------
① HERMETIC — the default. ~10 s, no Docker, no network, no model
  call. `sandbox._docker(*args)` is a recording fake daemon (images,
  containers, volumes, labels), so ensure_container's REAL argv reaches an
  assertion — every security property of the sandbox IS a flag in that argv.

② --docker — the real thing: builds the real image, starts a real
  container, and confirms its mounts match the contract §2 asserts on the
  argv. Skips with a stated reason when the daemon is down.

⚠ ISOLATION. Everything created here is named `zzsbx-*` (slug prefix), lives
under a throwaway ORGTREE_DATA, and is torn down in an atexit hook that
refuses to touch any name it did not create. Ports 7407 only. The user's live
orgs (`game-club`, `resonite`) are unsandboxed and are never loaded.

    §1  identity, config and paths            (no Docker)
    §2  the container contract                (fake daemon)
    §3  the bridge — the one door out         (ASGI + a real uvicorn on 7407)
    §5  storage enforcement is off for sandboxed orgs
    §6  the sandboxed turn
    §7  subproxy — the OAuth refresh
    §8  creation-time rules
    §9  real Docker                           (--docker only)

One defect was reproduced RED and FIXED in this suite's own files
(subproxy.py's never-updated `refreshTokenExpiresAt`); the rest are printed
as ⚑ notes at the end of every run because they live in files this agent may
not write. The run ends with a FIXED HERE and a REPORTED, NOT FIXED block —
read them, they are the point.
"""

from __future__ import annotations

import atexit
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time

sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

DOCKER_TIER = "--docker" in sys.argv
ONLY = None
if "--only" in sys.argv:
    ONLY = sys.argv[sys.argv.index("--only") + 1]

DATA = tempfile.mkdtemp(prefix="orgtree-sbxtest-")
os.environ["ORGTREE_DATA"] = DATA

# ⚠ a throwaway ORGTREE_DATA does NOT isolate the MAIL HUB: net._default_address
# falls back to net.DEFAULT_HUB_ADDRESS — the operator's real hub — when this
# root has no defaults.json, and any rig that starts the net daemon then
# registers its fixture orgs there permanently. Measured twice (user report
# 2026-08-06; ~45 fixture orgs again on 2026-08-10). The discard port refuses
# instantly, so registration fails harmlessly into the backoff.
# Guarded over this whole directory by test_external_mail §1.
os.makedirs(os.environ["ORGTREE_DATA"], exist_ok=True)
with open(os.path.join(os.environ["ORGTREE_DATA"], "defaults.json"), "w",
          encoding="utf-8") as _f:
    _f.write('{"net_hub_address": "http://127.0.0.1:9"}')

os.environ["ORGTREE_PORT"] = "7407"
os.environ["ORGTREE_BRIDGE_PORT"] = "7407"
os.environ["ORGTREE_PUBLIC_PORT"] = "7407"
os.environ.pop("ORGTREE_SANDBOX_MCP", None)
os.environ.pop("ORGTREE_SANDBOX_API_KEY", None)
os.environ.pop("ORGTREE_EXPOSE_ADMIN", None)

from orgtree import (api, sandbox, store, subproxy,              # noqa: E402
                     supervisor)
from orgtree.ledger import USER                                  # noqa: E402


# ---- the blast-radius guard. Nothing in this file may name a slug that does
# not start with this prefix, and the real credentials file may never be the
# one subproxy writes to.
PFX = "zzsbx-"
REAL_CREDS = os.path.expanduser("~/.claude/.credentials.json")
assert DATA != os.path.expanduser("~/orgtree"), "refusing to run on the real data root"

supervisor.chatq_register_org = lambda slug: None
supervisor.chatq_deregister_org = lambda slug: None
_warmed: list[str] = []
REAL_WARM = sandbox.warm          # §2 drives the real one; §8 counts calls
sandbox.warm = lambda org: _warmed.append(org.d["slug"])

PASS = 0
NOTES: list[str] = []
FIXED: list[str] = []
SKIPS: list[str] = []

def check(label, fn):
    global PASS
    fn()
    PASS += 1
    print(f"  ok {PASS:3d}  {label}")


def t(label):
    def deco(fn):
        check(label, fn)
        return fn
    return deco


def note(text):
    """A finding that is REPORTED, not fixed (it lives outside this suite's
    writable files). Printed at the end of every run so it cannot be lost."""
    NOTES.append(text)
    print(f"       ⚑ {text}")


def fixed(text):
    """A defect this suite reproduced AND fixed, in one of its own files."""
    FIXED.append(text)
    print(f"       ✓ FIXED: {text}")


def skip(text):
    SKIPS.append(text)
    print(f"       ○ SKIPPED: {text}")


def section(name):
    if ONLY and ONLY not in name:
        return False
    print(f"\n{name}")
    return True


# --------------------------------------------------------------- fixtures
def mkorg(name, *, kiosk=False, sandboxed=True, secret=None,
          kiosk_enabled=True, api_key=None):
    """An org doc straight through store (the create-path itself is §8)."""
    org = store.create_org(PFX + name)
    slug = org.d["slug"]
    assert slug.startswith(PFX), slug
    with store.DOC_LOCK:
        o = store.load_org(slug)
        if kiosk:
            o.d["kiosk"] = {"enabled": kiosk_enabled,
                            "token": "tok" + slug.replace("-", "_"),
                            "credits": 100, "spend_limit": 0.0,
                            "storage_limit_mb": 4096,
                            "sandbox": bool(sandboxed),
                            "sandbox_secret": secret or ("a1" * 16),
                            "auto_raise": False, "max_scope": None,
                            **({"api_key": api_key} if api_key else {})}
        elif sandboxed:
            o.d["sandbox"] = {"enabled": True, "secret": secret or ("b2" * 16)}
        store.save_org(o)
    return store.load_org(slug)


def drop(slug):
    try:
        store.delete_org(slug)
    except Exception:                                          # noqa: BLE001
        pass


@atexit.register
def _cleanup():
    for p in glob.glob(os.path.join(DATA, "orgs", "*.json")):
        s = os.path.basename(p)[:-5]
        if s.startswith(PFX):
            drop(s)
    shutil.rmtree(DATA, ignore_errors=True)


# ================================================================== §1
if section("§1  identity, config and paths"):
    O_KIOSK = mkorg("k1", kiosk=True, secret="c3" * 16)
    O_PLAIN = mkorg("p1", sandboxed=False)
    O_ORGSBX = mkorg("s1", secret="d4" * 16)

    @t("a kiosk with sandbox:true is sandboxed; its secret is the kiosk one")
    def _():
        assert sandbox.is_sandboxed(O_KIOSK)
        assert sandbox.sandbox_secret(O_KIOSK) == "c3" * 16

    @t("a normal org with sandbox.enabled is sandboxed too (user ruling)")
    def _():
        assert sandbox.is_sandboxed(O_ORGSBX)
        assert sandbox.sandbox_secret(O_ORGSBX) == "d4" * 16

    @t("a plain org is NOT sandboxed and has no secret")
    def _():
        assert not sandbox.is_sandboxed(O_PLAIN)
        assert sandbox.sandbox_secret(O_PLAIN) == ""

    @t("a kiosk with sandbox:false falls through to the org-level sandbox key")
    def _():
        o = mkorg("k2", kiosk=True, sandboxed=False)
        assert not sandbox.is_sandboxed(o)
        with store.DOC_LOCK:
            o2 = store.load_org(o.d["slug"])
            o2.d["sandbox"] = {"enabled": True, "secret": "e5" * 16}
            store.save_org(o2)
        assert sandbox.is_sandboxed(store.load_org(o.d["slug"]))
        drop(o.d["slug"])

    @t("sandbox.enabled:false is not a sandbox (the flag is read, not the key)")
    def _():
        o = mkorg("k3", sandboxed=False)
        with store.DOC_LOCK:
            o2 = store.load_org(o.d["slug"])
            o2.d["sandbox"] = {"enabled": False, "secret": "f6" * 16}
            store.save_org(o2)
        assert not sandbox.is_sandboxed(store.load_org(o.d["slug"]))
        drop(o.d["slug"])

    @t("container names and the in-container path mirror are stable")
    def _():
        s = O_KIOSK.d["slug"]
        assert sandbox.container_name(s) == "orgtree-" + s
        assert sandbox.cpath_data() == "/home/agent/orgtree"
        assert sandbox.cpath_workspace(s) == f"/home/agent/orgtree/workspaces/{s}"
        # steer.py derives identity from the cwd: <data>/scratch/<org>/<node>
        assert sandbox.cpath_scratch(s, "alice") == \
            f"/home/agent/orgtree/scratch/{s}/alice"

    @t("☞ a lineage id (name@gen) shares the BASE scratch dir, as on the host")
    def _():
        s = O_KIOSK.d["slug"]
        assert sandbox.cpath_scratch(s, "alice@3") == \
            sandbox.cpath_scratch(s, "alice")

    @t("exec_argv runs one command in the org's container, in one cwd")
    def _():
        assert sandbox.exec_argv("orgtree-x", "/w") == \
            ["docker", "exec", "-i", "-w", "/w", "orgtree-x"]

    @t("bridge_url points at the host gateway alias and the bridge port")
    def _():
        assert sandbox.bridge_url() == "http://host.docker.internal:7407"

    @t("uses_subscription_auth: proxied is the default, not credential copying")
    def _():
        assert not sandbox.uses_subscription_auth(None)
        assert not sandbox.uses_subscription_auth({})
        assert not sandbox.uses_subscription_auth({"api_key": "sk-ant-x"})
        assert sandbox.uses_subscription_auth({"api_key": "subscription"})
        assert sandbox.uses_subscription_auth({"api_key": " Subscription "})

    @t("uses_subscription_auth honours ORGTREE_SANDBOX_API_KEY")
    def _():
        os.environ["ORGTREE_SANDBOX_API_KEY"] = "subscription"
        try:
            assert sandbox.uses_subscription_auth({})
            # an explicit per-org key still wins over the env
            assert not sandbox.uses_subscription_auth({"api_key": "sk-ant-1"})
        finally:
            os.environ.pop("ORGTREE_SANDBOX_API_KEY")

    @t("sandbox_home is the host sandbox dir")
    def _():
        s = O_KIOSK.d["slug"]
        assert sandbox.sandbox_home(s) == \
            os.path.join(DATA, "sandboxes", s, "home")

    @t("the /usr/local volume name carries the CLI version AND the image rev")
    def _():
        assert sandbox.usrlocal_volume("2.1.220") == \
            f"orgtree-usrlocal-2.1.220-{sandbox.IMG_REV}"
        assert sandbox.usrlocal_volume("2.1.221") != \
            sandbox.usrlocal_volume("2.1.220"), "a CLI move must move the volume"


# ================================================================== §2
# A recording fake daemon. The point is not that Docker is simulated well —
# it is that `ensure_container`'s REAL argv reaches an assertion. Every
# security property of the sandbox is a flag in that argv.
class FakeDocker:
    def __init__(self):
        self.calls: list[list[str]] = []
        self.server_up = True
        self.images: set[str] = set()
        self.volumes: set[str] = set()
        self.containers: dict[str, dict] = {}
        self.build_fails = False
        self.run_fails = False
        self.migrate_output = "MIGRATED\n"
        self.timeouts: set[str] = set()      # first arg → raise TimeoutExpired

    def cp(self, args, rc=0, out="", err=""):
        return subprocess.CompletedProcess(args=list(args), returncode=rc,
                                           stdout=out, stderr=err)

    def find(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix)]

    def last_run(self):
        runs = [c for c in self.calls if c and c[0] == "run" and "-d" in c]
        assert runs, "no `docker run -d` was issued"
        return runs[-1]

    def __call__(self, *args, timeout=120):
        a = list(args)
        self.calls.append(a)
        if a and a[0] in self.timeouts:
            raise subprocess.TimeoutExpired(a, timeout)
        if a[:1] == ["version"]:
            return self.cp(a, 0 if self.server_up else 1, "28.0.0\n")
        if a[:2] == ["image", "inspect"]:
            if a[2] not in self.images:
                return self.cp(a, 1, "", f"No such image: {a[2]}")
            return self.cp(a, 0, json.dumps([{
                "Id": "sha256:" + "1" * 64, "Config": {"Labels": {}}}]))
        if a[:1] == ["build"]:
            if self.build_fails:
                return self.cp(a, 1, "", "no space left on device")
            self.images.add(a[a.index("-t") + 1])
            return self.cp(a, 0)
        if a[:2] == ["volume", "inspect"]:
            return self.cp(a, 0 if a[2] in self.volumes else 1)
        if a[:2] == ["volume", "create"]:
            self.volumes.add(a[2])
            return self.cp(a, 0)
        if a[:2] == ["volume", "rm"]:
            for v in a[3:]:
                self.volumes.discard(v)
            return self.cp(a, 0)
        if a[:2] == ["container", "inspect"]:
            name, fmt = a[-1], a[a.index("-f") + 1]
            c = self.containers.get(name)
            if not c:
                return self.cp(a, 1, "", f"No such container: {name}")
            out = fmt.replace("{{.State.Running}}",
                              "true" if c["running"] else "false")
            out = out.replace("{{.Config.Image}}", c["image"])
            # ANY label, not just orgtree.layout — the container's identity
            # grew an `orgtree.auth` label 2026-08-18 and a hardcoded
            # substitution silently answered "" for it, which reads as a
            # changed identity and recreates on every call
            out = re.sub(r'\{\{index \.Config\.Labels "([^"]+)"\}\}',
                         lambda m: c["labels"].get(m.group(1), "<no value>"),
                         out)
            return self.cp(a, 0, out + "\n")
        if a[:1] == ["run"] and "--rm" in a:
            return self.cp(a, 0, self.migrate_output)       # migration helper
        if a[:1] == ["run"]:
            if self.run_fails:
                return self.cp(a, 1, "", "invalid mount config")
            name = a[a.index("--name") + 1]
            labels = {}
            for i, x in enumerate(a):
                if x == "--label" and "=" in a[i + 1]:
                    k, _, v = a[i + 1].partition("=")
                    labels[k] = v
            image = next((x for x in a if x in self.images), a[-3])
            self.containers[name] = {"running": True, "image": image,
                                     "labels": labels}
            return self.cp(a, 0, "deadbeef\n")
        if a[:2] == ["rm", "-f"]:
            self.containers.pop(a[2], None)
            return self.cp(a, 0)
        if a[:1] == ["start"]:
            if a[1] in self.containers:
                self.containers[a[1]]["running"] = True
            return self.cp(a, 0)
        if a[:1] == ["stop"]:
            if a[-1] in self.containers:
                self.containers[a[-1]]["running"] = False
            return self.cp(a, 0)
        if a[:1] == ["exec"]:
            return self.cp(a, 0)
        return self.cp(a, 0)


if section("§2  the container contract"):
    FD = FakeDocker()
    sandbox._docker = FD
    supervisor.cli_version = lambda: "2.1.220"
    TAG = f"orgtree-sandbox:2.1.220-{sandbox.IMG_REV}"

    def fresh(name, **kw):
        FD.calls.clear()
        return mkorg(name, **kw)

    def mounts(argv):
        return [argv[i + 1] for i, x in enumerate(argv) if x == "-v"]

    @t("ensure_image builds a tag carrying the HOST CLI version + image rev")
    def _():
        FD.images.clear()
        FD.calls.clear()
        assert sandbox.ensure_image() == TAG
        b = FD.find("build")
        assert b and b[0][b[0].index("-t") + 1] == TAG, b
        assert "--build-arg" in b[0] and "CLAUDE_VERSION=2.1.220" in b[0], b
        assert b[0][-1] == os.path.join(sandbox.REPO_ROOT, "sandbox"), b

    @t("…and does NOT rebuild when the tag already exists")
    def _():
        FD.calls.clear()
        assert sandbox.ensure_image() == TAG
        assert not FD.find("build"), FD.calls

    @t("a failed image build raises with the daemon's own message")
    def _():
        FD.images.clear()
        FD.build_fails = True
        try:
            sandbox.ensure_image()
            raise AssertionError("build failure was swallowed")
        except RuntimeError as e:
            assert "no space left" in str(e), e
        finally:
            FD.build_fails = False
        sandbox.ensure_image()

    @t("☠ a stopped daemon is an actionable refusal, never a silent no-op")
    def _():
        o = fresh("c0")
        FD.server_up = False
        try:
            sandbox.ensure_container(o)
            raise AssertionError("ran without a daemon")
        except RuntimeError as e:
            assert "Docker is not running" in str(e), e
        finally:
            FD.server_up = True
        assert not FD.find("run"), "it tried to run something anyway"
        drop(o.d["slug"])

    # ---- the argv, which IS the security boundary
    ORG_A = fresh("c1", kiosk=True, secret="11" * 16)
    SLUG_A = ORG_A.d["slug"]
    NAME_A = sandbox.container_name(SLUG_A)
    assert sandbox.ensure_container(ORG_A) == NAME_A
    RUN = FD.last_run()
    HOME_A = sandbox.sandbox_home(SLUG_A)
    WS_A = ORG_A.d["workspace"]
    SCRATCH_A = store.scratch_root(SLUG_A)

    @t("☠ the rootfs runs READ-ONLY (writes land only in volumes and binds)")
    def _():
        assert "--read-only" in RUN, RUN

    @t("☠ /tmp and /run are RAM tmpfs, sized, and /tmp is 1777")
    def _():
        tm = [RUN[i + 1] for i, x in enumerate(RUN) if x == "--tmpfs"]
        assert f"/tmp:rw,size={sandbox.TMP_SIZE},mode=1777" in tm, tm
        assert f"/run:rw,size={sandbox.RUN_SIZE}" in tm, tm
        assert sandbox.TMP_SIZE and sandbox.RUN_SIZE, "unbounded tmpfs"

    @t("☠ CPU and memory are capped (the tmpfs bound rides --memory)")
    def _():
        assert RUN[RUN.index("--memory") + 1] == sandbox.MEM
        assert RUN[RUN.index("--cpus") + 1] == sandbox.CPUS

    @t("☠ system dirs are the org's OWN named volumes")
    def _():
        m = mounts(RUN)
        for d in sandbox.SYS_DIRS:
            assert f"{sandbox.sys_volume(SLUG_A, d)}:/{d}" in m, (d, m)

    @t("☠ home, workspace and scratch are the org's OWN host dirs")
    def _():
        m = mounts(RUN)
        assert f"{HOME_A}:/home/agent" in m, m
        assert f"{WS_A}:{sandbox.cpath_workspace(SLUG_A)}" in m, m
        assert f"{SCRATCH_A}:{sandbox.cpath_data()}/scratch/{SLUG_A}" in m, m
        for p in (HOME_A, WS_A, SCRATCH_A):
            assert os.path.isdir(p), p

    @t("☠ the ONLY other host bind is the backend, READ-ONLY")
    def _():
        own = {HOME_A, WS_A, SCRATCH_A}
        host = [s for s in mounts(RUN)
                if s.split(":", 1)[0] not in own and not s.startswith("orgtree-")]
        assert host == [f"{sandbox.BACKEND_DIR}:/opt/orgtree-backend:ro"], host
        # ⚠ the data root itself (org docs, every other org's dirs) is NOT
        # reachable from inside
        assert not any(s.split(":", 1)[0] == DATA for s in mounts(RUN)), \
            mounts(RUN)

    @t("a sandboxed org with no workspace recorded is refused, not bound")
    def _():
        o = fresh("c6", secret="66" * 16)
        with store.DOC_LOCK:
            o2 = store.load_org(o.d["slug"])
            o2.d.pop("workspace", None)
            store.save_org(o2)
        FD.calls.clear()
        try:
            sandbox.ensure_container(store.load_org(o.d["slug"]))
            raise AssertionError("ran with no workspace")
        except RuntimeError as e:
            assert "no workspace directory recorded" in str(e), e
        assert not FD.find("run"), FD.calls
        drop(o.d["slug"])

    @t("☠ /usr/local is the version-pinned READ-ONLY volume (the CLI is fixed)")
    def _():
        m = mounts(RUN)
        want = f"{sandbox.usrlocal_volume('2.1.220')}:/usr/local:ro"
        assert want in m, m

    @t("the layout label is stamped so a stale container is recreated, not run")
    def _():
        assert f"orgtree.layout={sandbox.LAYOUT}" in RUN, RUN

    @t("the container idles; turns arrive by exec")
    def _():
        assert RUN[-3:] == [TAG, "sleep", "infinity"], RUN[-4:]

    @t("host.docker.internal is mapped (the bridge is the one door out)")
    def _():
        assert RUN[RUN.index("--add-host") + 1] == \
            "host.docker.internal:host-gateway"

    @t("☠ proxied auth: the CLI's base URL carries the secret; no credential")
    def _():
        env = dict(RUN[i + 1].split("=", 1) for i, x in enumerate(RUN)
                   if x == "-e")
        assert env["ANTHROPIC_BASE_URL"] == \
            f"http://host.docker.internal:7407/anthropic/{'11' * 16}"
        assert env["ANTHROPIC_API_KEY"] == "orgtree-proxied"
        assert not any("sk-ant" in v for v in env.values()), env

    @t("☠ .bridge is the only secret in the container, in the sandbox home")
    def _():
        p = os.path.join(HOME_A, "orgtree", ".bridge")
        b = json.load(open(p, encoding="utf-8"))
        assert b == {"url": "http://host.docker.internal:7407",
                     "secret": "11" * 16}, b
        # steer.py reads <data-root>/.bridge with HOME=/home/agent ⇒
        # /home/agent/orgtree/.bridge — the file must sit exactly there
        assert p.endswith(os.path.join("home", "orgtree", ".bridge"))

    @t("a second call reuses the running container (no rm, no run)")
    def _():
        FD.calls.clear()
        assert sandbox.ensure_container(store.load_org(SLUG_A)) == NAME_A
        assert not FD.find("run") and not FD.find("rm"), FD.calls

    @t("an AUTH change recreates the container (the credential is baked in "
       "at `docker run`, and supervisor.bills_the_key trusts the config)")
    def _():
        # redteam 2026-08-18: nothing recreated a container when its auth
        # changed, so one created under ORGTREE_SANDBOX_API_KEY kept billing
        # that key after the var was unset — while `bills_the_key`, reading
        # today's config, called those turns "subscription" and timed the
        # container's own API limits against the host's lanes.
        FD.calls.clear()
        org = store.load_org(SLUG_A)
        assert sandbox.ensure_container(org) == NAME_A
        assert not FD.find("rm"), "a no-op call recreated the container"
        os.environ["ORGTREE_SANDBOX_API_KEY"] = "sk-ant-escape-hatch"
        try:
            FD.calls.clear()
            assert sandbox.ensure_container(store.load_org(SLUG_A)) == NAME_A
            assert FD.find("rm"), (
                "the auth changed and the container was reused: it is still "
                "running on the previous credential")
            lbl = [x for x in FD.last_run() if x.startswith("orgtree.auth=")]
            assert lbl and lbl[0].startswith("orgtree.auth=key:"), lbl
            assert "sk-ant-escape-hatch" not in " ".join(
                x for x in FD.last_run()
                if x.startswith("orgtree.auth=")), "the label leaks the key"
        finally:
            os.environ.pop("ORGTREE_SANDBOX_API_KEY", None)
        FD.calls.clear()
        assert sandbox.ensure_container(store.load_org(SLUG_A)) == NAME_A
        assert FD.find("rm"), "unsetting the key must recreate it too"

    @t("a STOPPED container is started, not recreated (state survives)")
    def _():
        FD.containers[NAME_A]["running"] = False
        FD.calls.clear()
        sandbox.ensure_container(store.load_org(SLUG_A))
        assert FD.find("start", NAME_A), FD.calls
        assert not FD.find("run"), "it recreated a stopped container"

    @t("☞ a CLI version move recreates the container (№44: the image is pinned)")
    def _():
        supervisor.cli_version = lambda: "2.1.221"
        FD.calls.clear()
        sandbox.ensure_container(store.load_org(SLUG_A))
        assert FD.find("rm", "-f", NAME_A), FD.calls
        run = FD.last_run()
        assert run[-3] == f"orgtree-sandbox:2.1.221-{sandbox.IMG_REV}", run[-3]
        assert f"{sandbox.usrlocal_volume('2.1.221')}:/usr/local:ro" in mounts(run)
        supervisor.cli_version = lambda: "2.1.220"

    @t("a container from an older LAYOUT is recreated too")
    def _():
        sandbox.ensure_container(store.load_org(SLUG_A))
        FD.containers[NAME_A]["labels"]["orgtree.layout"] = "volumes-v0"
        FD.calls.clear()
        sandbox.ensure_container(store.load_org(SLUG_A))
        assert FD.find("rm", "-f", NAME_A), FD.calls
        assert FD.last_run()[-3] == TAG

    @t("a container that fails to start raises with the daemon's message")
    def _():
        FD.run_fails = True
        FD.containers.pop(NAME_A, None)
        try:
            sandbox.ensure_container(store.load_org(SLUG_A))
            raise AssertionError("no raise")
        except RuntimeError as e:
            assert "failed to start" in str(e) and "invalid mount" in str(e), e
        finally:
            FD.run_fails = False

    # ---- auth modes
    @t("an explicit api_key replaces the proxy with a plain env key")
    def _():
        o = fresh("c2", kiosk=True, api_key="sk-ant-test-123")
        sandbox.ensure_container(o)
        run = FD.last_run()
        env = [run[i + 1] for i, x in enumerate(run) if x == "-e"]
        assert env == ["ANTHROPIC_API_KEY=sk-ant-test-123"], env
        drop(o.d["slug"])

    @t("☠ subscription auth + a PUBLIC kiosk URL is refused STRUCTURALLY")
    def _():
        o = fresh("c3", kiosk=True, api_key="subscription")
        FD.calls.clear()
        try:
            sandbox.ensure_container(o)
            raise AssertionError("copied host credentials into a public kiosk")
        except RuntimeError as e:
            assert "refused" in str(e) and "credentials" in str(e), e
        assert not FD.find("run"), FD.calls
        drop(o.d["slug"])

    @t("subscription auth with NO kiosk URL copies the credentials file in")
    def _():
        o = fresh("c4", secret="22" * 16)
        os.environ["ORGTREE_SANDBOX_API_KEY"] = "subscription"
        fake_home = os.path.join(DATA, "fakehome")
        os.makedirs(os.path.join(fake_home, ".claude"), exist_ok=True)
        with open(os.path.join(fake_home, ".claude", ".credentials.json"),
                  "w") as f:
            f.write('{"claudeAiOauth":{"accessToken":"HOST-TOKEN"}}')
        real_exp = os.path.expanduser
        os.path.expanduser = lambda p: (
            p.replace("~", fake_home) if p.startswith("~/.claude")
            else real_exp(p))
        try:
            sandbox.ensure_container(o)
            run = FD.last_run()
            assert not [x for i, x in enumerate(run) if x == "-e"], run
            dst = os.path.join(sandbox.sandbox_home(o.d["slug"]), ".claude",
                               ".credentials.json")
            assert "HOST-TOKEN" in open(dst, encoding="utf-8").read()
        finally:
            os.path.expanduser = real_exp
            os.environ.pop("ORGTREE_SANDBOX_API_KEY")
        drop(o.d["slug"])

    @t("subscription auth with no credentials file on the host is refused")
    def _():
        o = fresh("c5", secret="33" * 16)
        os.environ["ORGTREE_SANDBOX_API_KEY"] = "subscription"
        real_exp = os.path.expanduser
        os.path.expanduser = lambda p: (os.path.join(DATA, "nope", p[2:])
                                        if p.startswith("~/") else real_exp(p))
        try:
            sandbox.ensure_container(o)
            raise AssertionError("ran with no credentials")
        except RuntimeError as e:
            assert "no Claude credentials found" in str(e), e
        finally:
            os.path.expanduser = real_exp
            os.environ.pop("ORGTREE_SANDBOX_API_KEY")
        drop(o.d["slug"])

    # ---- teardown levers
    @t("☞ kill_claude reaps ONE turn's process, not every agent's (№40)")
    def _():
        FD.calls.clear()
        sandbox.kill_claude(NAME_A, "session-abc")
        assert FD.calls[-1] == ["exec", NAME_A, "pkill", "-9", "-f",
                                "session-abc"], FD.calls[-1]
        # the default is the blunt one — callers in the turn loop pass the sid
        FD.calls.clear()
        sandbox.kill_claude(NAME_A)
        assert FD.calls[-1][-1] == "claude"

    @t("kill_claude survives a wedged daemon (a timeout is not an exception)")
    def _():
        FD.timeouts.add("exec")
        try:
            sandbox.kill_claude(NAME_A, "x")
        finally:
            FD.timeouts.discard("exec")

    @t("remove() tears down the container and its legacy volumes")
    def _():
        o = fresh("r1", secret="55" * 16)
        s = o.d["slug"]
        sandbox.ensure_container(o)
        FD.calls.clear()
        sandbox.remove(s)
        assert FD.find("rm", "-f", sandbox.container_name(s)), FD.calls
        vrm = FD.find("volume", "rm", "-f")[0]
        assert set(vrm[3:]) == {sandbox.sys_volume(s, d)
                                for d in sandbox.SYS_DIRS}, vrm
        drop(s)

    @t("☞ remove() tombstones the slug so a racing warm() cannot leak it")
    def _():
        o = fresh("r2", secret="56" * 16)
        s = o.d["slug"]
        sandbox.remove(s)
        assert s in sandbox._dead
        gate = threading.Event()
        real = sandbox.ensure_container
        sandbox.ensure_container = lambda org: (gate.wait(5),
                                                real(org))[1]
        try:
            REAL_WARM(o)                    # background prebuild starts…
            sandbox.remove(s)               # …org deleted mid-build
            FD.calls.clear()
            gate.set()
            for _ in range(100):
                if FD.find("rm", "-f", sandbox.container_name(s)):
                    break
                time.sleep(0.05)
            assert FD.find("rm", "-f", sandbox.container_name(s)), \
                "the container built after the delete was leaked"
        finally:
            sandbox.ensure_container = real
        drop(s)

    @t("warm() never propagates its failure (the turn surfaces it instead)")
    def _():
        o = fresh("r3", secret="57" * 16)
        FD.server_up = False
        try:
            REAL_WARM(o)
            time.sleep(0.3)
        finally:
            FD.server_up = True
        drop(o.d["slug"])

# ================================================================== §3
# The bridge is the ONE door out of the container. Requests are made by
# calling the ASGI app directly with a hand-built scope (the same technique
# test_api_surface documents): a client would normalise `..`, `%2f` and case
# before the gateway ever saw them, and the gateway is the thing under test.
BRIDGE = api.BridgeGateway(api.app)
ADMIN = api.app
PUBLIC = api.PublicGateway(api.app)


class Res:
    def __init__(self, status, body, exc=None):
        self.status, self.body, self.exc = status, body, exc
        try:
            self.json = json.loads(body)
        except Exception:                                      # noqa: BLE001
            self.json = None

    @property
    def text(self):
        return self.body.decode("utf-8", "replace")

    def __repr__(self):
        return f"<{self.status} {(self.exc or self.text)[:180]!r}>"


def call(app, method, path, body=None, headers=None, query=b""):
    import asyncio
    payload = b"" if body is None else json.dumps(body).encode()
    hdrs = [(b"host", b"127.0.0.1:7407")]
    if payload:
        hdrs += [(b"content-type", b"application/json"),
                 (b"content-length", str(len(payload)).encode())]
    for k, v in (headers or []):
        hdrs.append((k.lower().encode(), v.encode()))
    st, chunks, exc = [0], [], [None]

    async def receive():
        return {"type": "http.request", "body": payload, "more_body": False}

    async def send(msg):
        if msg["type"] == "http.response.start":
            st[0] = msg["status"]
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))

    scope = {"type": "http", "asgi": {"version": "3.0", "spec_version": "2.3"},
             "http_version": "1.1", "method": method, "scheme": "http",
             "path": path, "raw_path": path.encode(), "query_string": query,
             "root_path": "", "headers": hdrs, "client": ("127.0.0.1", 5555),
             "server": ("127.0.0.1", 7407)}
    try:
        asyncio.run(app(scope, receive, send))
    except Exception as e:                                     # noqa: BLE001
        exc[0] = f"{type(e).__name__}: {e}"
    return Res(st[0], b"".join(chunks), exc[0])


if section("§3  the bridge — the one door out"):
    SEC_A = "1a" * 16
    SEC_B = "2b" * 16
    OB1 = mkorg("b1", kiosk=True, secret=SEC_A)
    OB2 = mkorg("b2", secret=SEC_B)
    SB1, SB2 = OB1.d["slug"], OB2.d["slug"]
    for _s in (SB1, SB2):
        with store.DOC_LOCK:
            _o = store.load_org(_s)
            _o.hire(USER, None, "sonnet", 10, "alice", charter="c")
            store.save_org(_o)
    api._bridge_cache["at"] = 0.0

    def br(method, path, body=None, secret=SEC_A):
        h = [("x-orgtree-bridge", secret)] if secret is not None else []
        return call(BRIDGE, method, path, body, headers=h)

    def forbidden(r, what):
        assert r.status == 403 and r.json == {"detail": "forbidden"}, \
            f"{what}: expected a bare bridge 403, got {r!r}"

    @t("the org's own secret opens /api/agent for its own org")
    def _():
        r = br("POST", "/api/agent",
               {"org": SB1, "node": "alice", "tool": "orgtree_chart"})
        assert r.status == 200 and "chart" in (r.json or {}), r

    @t("☠ a secret is scoped to ITS org — another org's is a refusal")
    def _():
        r = br("POST", "/api/agent",
               {"org": SB2, "node": "alice", "tool": "orgtree_chart"})
        assert r.status == 403 and "scoped to its own org" in r.text, r

    @t("☠ the steer fetch is scoped the same way")
    def _():
        assert br("POST", f"/api/orgs/{SB1}/nodes/alice/steer").status == 200
        forbidden(br("POST", f"/api/orgs/{SB2}/nodes/alice/steer"),
                  "cross-org steer")

    @t("a made-up secret opens nothing")
    def _():
        for bad in ["0" * 32, "", "  ", SEC_A[:-1], SEC_A + "f", SEC_A.upper(),
                    "'; DROP TABLE", None]:
            forbidden(br("POST", "/api/agent", {"org": SB1, "node": "alice",
                                                "tool": "orgtree_chart"},
                         secret=bad), f"secret={bad!r}")

    @t("☠ a DELETED org's secret stops working (the map is rebuilt, not kept)")
    def _():
        o = mkorg("b3", secret="3c" * 16)
        api._bridge_cache["at"] = 0.0
        assert api._bridge_secret_map().get("3c" * 16) == o.d["slug"]
        drop(o.d["slug"])
        api._bridge_cache["at"] = 0.0
        assert "3c" * 16 not in api._bridge_secret_map()
        forbidden(br("POST", "/api/agent", {"org": o.d["slug"], "node": "a",
                                            "tool": "orgtree_chart"},
                     secret="3c" * 16), "deleted org's secret")

    BRIDGE_CLOSED = [
        ("GET", "/api/agent"), ("PUT", "/api/agent"), ("PATCH", "/api/agent"),
        ("DELETE", "/api/agent"), ("GET", "/api/orgs"), ("GET", "/api/fs"),
        ("GET", "/api/host"), ("GET", "/api/defaults"), ("GET", "/api/mcp"),
        ("GET", f"/api/orgs/{SB1}"), ("POST", f"/api/orgs/{SB1}/settings"),
        ("POST", f"/api/orgs/{SB1}/ops"), ("DELETE", f"/api/orgs/{SB1}"),
        ("GET", f"/api/orgs/{SB1}/nodes/alice/chat"),
        ("POST", f"/api/orgs/{SB1}/nodes/alice/message"),
        ("GET", f"/api/orgs/{SB1}/nodes/alice/scratch"),
        ("GET", "/"), ("GET", "/index.html"), ("GET", "/openapi.json"),
        ("GET", "/docs"), ("POST", "/api/orgs"),
        # the two sanctioned paths, in shapes that are NOT them
        ("POST", "/api/agent/"), ("POST", "//api/agent"),
        ("POST", "/api/agent/x"), ("POST", " /api/agent"),
        ("POST", f"/api/orgs/{SB1}/nodes/alice/steer/"),
        ("POST", f"/api/orgs/{SB1}/nodes/alice/steer?x=1"),
        ("POST", f"/api/orgs/{SB1}/../{SB2}/nodes/alice/steer"),
        ("POST", "/anthropic"), ("POST", "/anthropic/"),
        ("POST", f"/anthropic{SEC_A}/v1/messages"),
    ]

    def _closed(m, p):
        def go():
            forbidden(br(m, p, {}), f"bridge {m} {p}")
        return go

    for _m, _p in BRIDGE_CLOSED:
        check(f"☠ bridge serves nothing at {_m} {_p}", _closed(_m, _p))

    # ---- /anthropic: the proxy that attaches the HOST's OAuth token
    _TOKEN_CALLS = []

    def _fake_token():
        _TOKEN_CALLS.append(time.time())
        raise RuntimeError("upstream-token-not-fetched-in-tests")

    _REAL_GET_TOKEN = subproxy.get_access_token
    subproxy.get_access_token = _fake_token

    @t("☠ /anthropic is UNREACHABLE on the admin listener (no bridge marker)")
    def _():
        _TOKEN_CALLS.clear()
        r = call(ADMIN, "POST", "/anthropic/v1/messages", {"model": "x"})
        assert r.status == 403 and "bridge only" in r.text, r
        assert not _TOKEN_CALLS, "the host token was fetched for a non-bridge"

    @t("☠ …and on the public kiosk listener")
    def _():
        _TOKEN_CALLS.clear()
        tok = store.load_org(SB1).d["kiosk"]["token"]
        api._token_cache["at"] = 0.0
        r = call(PUBLIC, "POST", f"/k/{tok}/anthropic/v1/messages", {"m": 1})
        assert r.status in (403, 404), r
        assert not _TOKEN_CALLS, "a kiosk visitor drove the OAuth proxy"

    @t("a valid secret in the PATH reaches the proxy (and only then)")
    def _():
        _TOKEN_CALLS.clear()
        r = br("POST", f"/anthropic/{SEC_A}/v1/messages", {"model": "x"},
               secret=None)
        assert r.status == 502 and "token-not-fetched" in r.text, r
        assert len(_TOKEN_CALLS) == 1, _TOKEN_CALLS

    @t("☠ a wrong/short/uppercase path secret never reaches the proxy")
    def _():
        _TOKEN_CALLS.clear()
        for bad in ["0" * 32, SEC_A[:31], SEC_A + "0", SEC_A.upper(),
                    "g" * 32, SEC_A[:16] + "-" + SEC_A[17:]]:
            forbidden(br("POST", f"/anthropic/{bad}/v1/messages", {},
                         secret=None), f"path secret {bad!r}")
        assert not _TOKEN_CALLS, _TOKEN_CALLS

    @t("☠ the rewritten /anthropic path cannot walk back into the API")
    def _():
        _TOKEN_CALLS.clear()
        r = br("POST", f"/anthropic/{SEC_A}/../api/agent",
               {"org": SB2, "node": "alice", "tool": "orgtree_chart"},
               secret=None)
        assert r.status != 200 or "chart" not in r.text, r
        assert "chart" not in r.text, r

    @t("the header secret ALSO opens the proxy (same gate, either carrier)")
    def _():
        _TOKEN_CALLS.clear()
        r = br("POST", "/anthropic/v1/messages", {"model": "x"}, secret=SEC_A)
        assert r.status == 403, ("a header-only /anthropic call is refused: "
                                 "the secret must ride the path", r)

    @t("bridge: a websocket is closed, never upgraded")
    def _():
        import asyncio
        closed = []

        async def send(msg):
            closed.append(msg)

        async def receive():
            return {"type": "websocket.connect"}

        asyncio.run(BRIDGE({"type": "websocket", "path": "/ws",
                            "headers": [(b"x-orgtree-bridge",
                                         SEC_A.encode())]}, receive, send))
        assert closed and closed[0]["type"] == "websocket.close", closed

    @t("bridge: lifespan is answered locally (the admin server owns the app)")
    def _():
        import asyncio
        msgs = [{"type": "lifespan.startup"}, {"type": "lifespan.shutdown"}]
        sent = []

        async def receive():
            return msgs.pop(0)

        async def send(m):
            sent.append(m["type"])

        asyncio.run(BRIDGE({"type": "lifespan"}, receive, send))
        assert sent == ["lifespan.startup.complete",
                        "lifespan.shutdown.complete"], sent

    @t("☠ the sandbox secret is in NO payload — not even on the admin listener")
    def _():
        for path in (f"/api/orgs/{SB1}", f"/api/orgs/{SB1}/settings",
                     "/api/orgs", f"/api/orgs/{SB1}/audiences",
                     f"/api/orgs/{SB1}/nodes/alice/chat"):
            r = call(ADMIN, "GET", path)
            assert SEC_A not in r.text, (path, r.text[:200])

    @t("☞ …and the org doc is the ONLY host-side copy of it")
    def _():
        hits = []
        for root, _dirs, files in os.walk(DATA):
            for f in files:
                p = os.path.join(root, f)
                try:
                    if SEC_A in open(p, encoding="utf-8",
                                     errors="ignore").read():
                        hits.append(os.path.relpath(p, DATA))
                except OSError:
                    pass
        assert hits, "the secret was found NOWHERE — the probe stopped working"
        if store.STORE_BACKEND == "sqlite":
            # SQLite EQUIVALENT: the org document is `orgs/<slug>.db` plus its
            # `-wal`/`-shm` sidecars, which are part of that same database (a
            # secret written but not yet checkpointed lives in the WAL). Any
            # OTHER file holding it is the leak this check exists to catch and
            # is still a failure.
            allowed = {os.path.join("orgs", SB1 + ".db") + x
                       for x in ("", "-wal", "-shm")}
            assert set(hits) <= allowed, hits
        else:
            assert hits == [os.path.join("orgs", SB1 + ".json")], hits


# ================================================================== §5
if section("§5  storage enforcement is off for sandboxed orgs"):
    @t("storage_check enforces nothing for a sandboxed org, even over its limit")
    def _():
        o = mkorg("cap1", kiosk=True, secret="5e" * 16)
        slug = o.d["slug"]
        real_usage = supervisor.workspace_usage_bytes
        supervisor.workspace_usage_bytes = lambda org, max_age=0.0: 1 << 40
        try:
            assert supervisor.storage_check(slug) is None
        finally:
            supervisor.workspace_usage_bytes = real_usage
        d = store.load_org(slug).d
        assert not d.get("storage_blocked") and not d.get("storage_warned"), d
        drop(slug)

    @t("storage_check clears storage flags a sandboxed org doc still carries")
    def _():
        """Docs written under the retired per-org disk can hold these flags,
        and nothing else clears them for a sandboxed org — a stuck
        `storage_blocked` refuses uploads and outbox copies forever."""
        o = mkorg("cap2", kiosk=True, secret="5f" * 16)
        slug = o.d["slug"]
        with store.DOC_LOCK:
            o2 = store.load_org(slug)
            o2.d["storage_blocked"] = True
            o2.d["storage_warned"] = True
            o2.d["storage_full"] = True
            store.save_org(o2)
        assert supervisor.storage_check(slug) is None
        d = store.load_org(slug).d
        for flag in ("storage_blocked", "storage_warned", "storage_full"):
            assert flag not in d, (flag, d)
        drop(slug)

    @t("☞ no turn gate pairs storage_blocked with a sandbox check")
    def _():
        """Sandboxed orgs never get `storage_blocked`, so a gate keyed on
        the flag AND sandbox state is dead code. A drift guard: the flag's
        remaining readers must not grow a sandbox-only branch back."""
        here = os.path.dirname(supervisor.__file__)
        for f in ("supervisor.py", "warmpool.py"):
            src = open(os.path.join(here, f), encoding="utf-8").read()
            assert not re.search(r'storage_blocked"\)\s+and\s+\w*\.?sbx\.', src), f


# ================================================================== §6
if section("§6  the sandboxed turn"):
    O6 = mkorg("turn1", kiosk=True, secret="6a" * 16)
    S6 = O6.d["slug"]
    with store.DOC_LOCK:
        _o = store.load_org(S6)
        _o.hire(USER, None, "sonnet", 20, "alice", charter="c")
        _o.hire(USER, "alice", "sonnet", 5, "bob", charter="c",
                add_dirs=[], tools={"bash": True, "web": True, "edit": True,
                                    "subagents": True, "mcp": []},
                org_visibility="team")
        store.save_org(_o)
    O6 = store.load_org(S6)
    SBXHOME = os.path.join(DATA, "sandboxes", S6, "home")

    @t("an unsandboxed org has no transcript root (the host default applies)")
    def _():
        assert supervisor._transcript_root(store.load_org(O_PLAIN.d["slug"])) \
            is None

    @t("☞ a sandboxed org's transcripts live under <data>/sandboxes/<slug>/home")
    def _():
        assert supervisor._transcript_root(O6) == \
            os.path.join(SBXHOME, ".claude")

    @t("transcript_path finds a session under that root and nowhere else")
    def _():
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        d = os.path.join(SBXHOME, ".claude", "projects", "proj")
        os.makedirs(d, exist_ok=True)
        open(os.path.join(d, sid + ".jsonl"), "w").write("{}\n")
        assert supervisor.transcript_path(sid, None) is None
        assert supervisor.transcript_path(
            sid, supervisor._transcript_root(O6)) == \
            os.path.join(d, sid + ".jsonl")

    @t("☞ REGRESSION: reconcile does NOT condemn a sandboxed node whose "
       "transcript is on the sandbox home")
    def _():
        """Omitting the org's transcript root here condemned EVERY sandboxed
        node at every restart — they resume from a home the host default
        never sees."""
        sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
        with store.DOC_LOCK:
            o = store.load_org(S6)
            o.node("alice")["session_id"] = sid
            o.node("alice")["cost_usd"] = 0.42
            store.save_org(o)
        assert supervisor.reconcile(S6) == []
        assert store.load_org(S6).node("alice")["state"] == "live"

    @t("…and DOES condemn one whose transcript is genuinely gone")
    def _():
        with store.DOC_LOCK:
            o = store.load_org(S6)
            o.node("bob")["session_id"] = "dead0000-0000-0000-0000-000000000000"
            o.node("bob")["cost_usd"] = 0.10
            store.save_org(o)
        assert supervisor.reconcile(S6) == ["bob"]
        assert store.load_org(S6).node("bob")["state"] != "live"

    # ---- the turn command line
    CMD = supervisor._build_cmd(store.load_org(S6), "alice")

    @t("☠ the turn runs INSIDE the container, in the node's own scratch dir")
    def _():
        assert CMD[:5] == ["docker", "exec", "-i", "-w",
                           sandbox.cpath_scratch(S6, "alice")], CMD[:6]
        assert CMD[5] == sandbox.container_name(S6), CMD[5]
        assert CMD[6] == "claude", CMD[6]

    @t("the steering hook runs the read-only backend mount's python3")
    def _():
        st = json.loads(CMD[CMD.index("--settings") + 1])
        hook = st["hooks"]["PostToolUse"][0]["hooks"][0]["command"]
        assert hook == ("python3 /opt/orgtree-backend/orgtree/steer.py "
                        f'"{S6}" "alice"'), hook
        assert st["hooks"]["SessionStart"] == [], \
            "the operator's global hooks must not leak into an agent"

    @t("☠ NO MCP server reaches a sandbox but orgtree, over the bridge")
    def _():
        cfg = json.loads(CMD[CMD.index("--mcp-config") + 1])["mcpServers"]
        assert list(cfg) == ["orgtree"], cfg
        o = cfg["orgtree"]
        assert o["command"] == "python3"
        assert o["args"] == ["/opt/orgtree-backend/orgtree/mcptool.py"]
        assert o["env"]["ORGTREE_BASE"] == sandbox.bridge_url()
        assert o["env"]["ORGTREE_BRIDGE_SECRET"] == "6a" * 16
        assert "PYTHONPATH" not in o["env"] and "ORGTREE_PORT" not in o["env"], \
            "a host path or the admin port would be a lie inside the container"

    @t("only container paths are handed to the agent — no host path anywhere")
    def _():
        adds = [CMD[i + 1] for i, x in enumerate(CMD) if x == "--add-dir"]
        assert adds and all(a.startswith("/home/agent/orgtree/") for a in adds), \
            adds
        joined = " ".join(CMD)
        assert DATA not in joined, "the host data root leaked into the argv"
        assert "C:\\" not in joined and "c:\\" not in joined.lower(), joined[:400]

    @t("☞ a §7.6 read-down grant reaches the descendant's CONTAINER scratch")
    def _():
        adds = [CMD[i + 1] for i, x in enumerate(CMD) if x == "--add-dir"]
        # D-201/S2(a): the read-down is one fixed parent path now, not one
        # entry per descendant, so bob is reached by COVERAGE rather than by
        # being named. The assertion tests the same property it always did —
        # can the agent's file tools reach its report's container scratch —
        # and deliberately does not care which shape delivers it. Exact-match
        # here would have been an assertion about the implementation.
        want = sandbox.cpath_scratch(S6, "bob")
        assert any(want == a or want.startswith(a.rstrip("/") + "/")
                   for a in adds), (want, adds)

    @t("an external folder grant cannot follow into the container")
    def _():
        ext = os.path.join(DATA, "outside-folder")
        os.makedirs(ext, exist_ok=True)
        with store.DOC_LOCK:
            o = store.load_org(S6)
            o.node("alice")["scope"]["add_dirs"].append({"path": ext,
                                                         "mode": "rw"})
            store.save_org(o)
        cmd = supervisor._build_cmd(store.load_org(S6), "alice")
        adds = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--add-dir"]
        assert ext not in adds and all(a.startswith("/home/agent/") for a in adds)

    @t("the workspace grant maps onto the container's ONE window")
    def _():
        ws = store.load_org(S6).d["workspace"]
        with store.DOC_LOCK:
            o = store.load_org(S6)
            o.node("alice")["scope"]["add_dirs"] = [{"path": ws, "mode": "rw"}]
            store.save_org(o)
        cmd = supervisor._build_cmd(store.load_org(S6), "alice")
        adds = [cmd[i + 1] for i, x in enumerate(cmd) if x == "--add-dir"]
        assert sandbox.cpath_workspace(S6) in adds, adds

    @t("a read-only workspace grant still writes deny rules, in container terms")
    def _():
        ws = store.load_org(S6).d["workspace"]
        with store.DOC_LOCK:
            o = store.load_org(S6)
            o.node("alice")["scope"]["add_dirs"] = [{"path": ws, "mode": "ro"}]
            store.save_org(o)
        cmd = supervisor._build_cmd(store.load_org(S6), "alice")
        st = json.loads(cmd[cmd.index("--settings") + 1])
        deny = st["permissions"]["deny"]
        # D-220: one Edit() rule per path — the only deny shape the CLI matches
        assert f"Edit({sandbox.cpath_workspace(S6)}/**)" in deny, deny
        with store.DOC_LOCK:
            o = store.load_org(S6)
            o.node("alice")["scope"]["add_dirs"] = [{"path": ws, "mode": "rw"}]
            store.save_org(o)

    @t("the sandboxed identity prompt tells the agent it is in a container")
    def _():
        p = supervisor.identity_prompt(store.load_org(S6), "alice")
        assert "sandbox container" in p, p[:400]
        assert "Terminal: Bash." in p, "PowerShell would be a lie on Linux"

    # ---- the MCP escape hatch
    REG = {"weather": {"url": "http://localhost:9099/mcp"},
           "fs": {"command": "/usr/bin/npx", "args": ["-y", "@x/fs"]},
           "native": {"command": "/opt/tools/thing", "args": []},
           "py": {"command": "python3", "args": ["-m", "srv"]}}

    @t("☠ by default a sandbox gets NOTHING from the MCP registry")
    def _():
        assert supervisor.sandbox_mcp_passthrough(list(REG), REG) == {}
        assert not supervisor.sandbox_mcp_enabled()

    @t("ORGTREE_SANDBOX_MCP passes URL + portable-stdio servers only")
    def _():
        os.environ["ORGTREE_SANDBOX_MCP"] = "1"
        try:
            out = supervisor.sandbox_mcp_passthrough(list(REG), REG)
            assert out["weather"]["url"] == "http://host.docker.internal:9099/mcp"
            assert out["fs"]["command"] == "npx" and \
                out["fs"]["args"] == ["-y", "@x/fs"], out["fs"]
            assert "py" in out and "native" not in out, out
            assert REG["weather"]["url"] == "http://localhost:9099/mcp", \
                "the registry itself was mutated"
        finally:
            os.environ.pop("ORGTREE_SANDBOX_MCP")

    @t("…and a server that was never GRANTED is still not passed")
    def _():
        os.environ["ORGTREE_SANDBOX_MCP"] = "1"
        try:
            assert supervisor.sandbox_mcp_passthrough(["weather"], REG) == \
                {"weather": {"url": "http://host.docker.internal:9099/mcp"}}
        finally:
            os.environ.pop("ORGTREE_SANDBOX_MCP")

    @t("with the flag on, the turn really carries the extra server")
    def _():
        os.environ["ORGTREE_SANDBOX_MCP"] = "1"
        real_reg = supervisor.registered_mcp_servers
        supervisor.registered_mcp_servers = lambda: REG
        try:
            with store.DOC_LOCK:
                o = store.load_org(S6)
                o.node("alice")["scope"]["tools"]["mcp"] = ["weather"]
                store.save_org(o)
            cmd = supervisor._build_cmd(store.load_org(S6), "alice")
            cfg = json.loads(cmd[cmd.index("--mcp-config") + 1])["mcpServers"]
            assert set(cfg) == {"orgtree", "weather"}, cfg
            assert "mcp__weather" in cmd[cmd.index("--allowedTools") + 1]
        finally:
            supervisor.registered_mcp_servers = real_reg
            os.environ.pop("ORGTREE_SANDBOX_MCP")
            with store.DOC_LOCK:
                o = store.load_org(S6)
                o.node("alice")["scope"]["tools"]["mcp"] = []
                store.save_org(o)

    # ---- container→host translation of agent-supplied dirs
    @t("☞ a sandboxed agent's own container paths are accepted as grants")
    def _():
        o = store.load_org(S6)
        ws = o.d["workspace"]
        dirs, warns = supervisor.sandbox_dirs_to_host(
            o, [{"path": sandbox.cpath_workspace(S6) + "/sub", "mode": "ro"}])
        assert dirs == [{"path": os.path.normpath(ws + "/sub"),
                         "mode": "ro"}], dirs
        assert warns == []

    @t("a scratch path is DROPPED with a warning (it is always reachable)")
    def _():
        o = store.load_org(S6)
        dirs, warns = supervisor.sandbox_dirs_to_host(
            o, [sandbox.cpath_scratch(S6, "bob")])
        assert dirs == [] and warns and "scratch" in warns[0], (dirs, warns)

    @t("anything else passes through to meet the honest №30 refusal")
    def _():
        o = store.load_org(S6)
        dirs, warns = supervisor.sandbox_dirs_to_host(o, ["/etc"])
        assert dirs == [{"path": "/etc", "mode": "rw"}] and warns == []

    @t("an UNSANDBOXED org is never translated")
    def _():
        o = store.load_org(O_PLAIN.d["slug"])
        assert supervisor.sandbox_dirs_to_host(o, ["C:\\x"]) == (["C:\\x"], [])

    # ---- steering into the container, over the real bridge listener
    @t("☞ steer.py inside a container reaches the node through the bridge")
    def _():
        import uvicorn
        cfg = uvicorn.Config(api.BridgeGateway(api.app), host="127.0.0.1",
                             port=7407, log_level="error")
        server = uvicorn.Server(cfg)
        th = threading.Thread(target=server.run, daemon=True)
        th.start()
        for _ in range(100):
            if getattr(server, "started", False):
                break
            time.sleep(0.05)
        assert server.started, "the bridge listener never came up"
        api._bridge_cache["at"] = 0.0
        try:
            # the container's view: ~/orgtree with a .bridge in its root
            croot = os.path.join(DATA, "cbox", "orgtree")
            os.makedirs(os.path.join(croot, "scratch", S6, "alice"),
                        exist_ok=True)
            with open(os.path.join(croot, ".bridge"), "w") as f:
                json.dump({"url": "http://127.0.0.1:7407",
                           "secret": "6a" * 16}, f)
            supervisor.state(S6, "alice")["steer"] = ["mail one", "mail two"]
            env = dict(os.environ, ORGTREE_DATA=croot)
            r = subprocess.run(
                [sys.executable,
                 os.path.join(os.path.dirname(supervisor.__file__), "steer.py"),
                 S6, "alice"],
                cwd=os.path.join(croot, "scratch", S6, "alice"),
                capture_output=True, text=True, env=env, timeout=30, input="")
            assert r.returncode == 0, r.stderr[-400:]
            assert r.stdout.strip(), (r.stdout, r.stderr[-500:])
            out = json.loads(r.stdout)["hookSpecificOutput"]
            assert "mail one" in out["additionalContext"], out
            assert "mail two" in out["additionalContext"], out
            assert supervisor.state(S6, "alice")["steer"] == [], \
                "the fetch is the delivery point — it must drain"

            # ☠ the same hook with a WRONG secret gets nothing at all
            with open(os.path.join(croot, ".bridge"), "w") as f:
                json.dump({"url": "http://127.0.0.1:7407", "secret": "0" * 32},
                          f)
            supervisor.state(S6, "alice")["steer"] = ["secret mail"]
            r = subprocess.run(
                [sys.executable,
                 os.path.join(os.path.dirname(supervisor.__file__), "steer.py"),
                 S6, "alice"],
                cwd=os.path.join(croot, "scratch", S6, "alice"),
                capture_output=True, text=True, env=env, timeout=30, input="")
            assert r.returncode == 0 and r.stdout.strip() == "", r.stdout
            assert supervisor.state(S6, "alice")["steer"] == ["secret mail"], \
                "an unauthorised hook consumed the mail"
        finally:
            server.should_exit = True
            th.join(timeout=20)

    @t("a container hook with NO .bridge falls back to loopback, not silence")
    def _():
        croot = os.path.join(DATA, "cbox2", "orgtree")
        os.makedirs(os.path.join(croot, "scratch", S6, "alice"), exist_ok=True)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "steermod", os.path.join(os.path.dirname(supervisor.__file__),
                                     "steer.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        old = os.getcwd()
        os.environ["ORGTREE_DATA"] = croot
        try:
            os.chdir(os.path.join(croot, "scratch", S6, "alice"))
            sys.argv = ["steer.py"]
            org, node, base, secret = mod.identity()
            assert (org, node) == (S6, "alice"), (org, node)
            assert base == "http://127.0.0.1:7407" and secret == "", base
        finally:
            os.chdir(old)
            os.environ["ORGTREE_DATA"] = DATA


# ================================================================== §7
# The OAuth refresh that keeps every proxied sandbox running. ⚠ The host's
# REAL credentials file is never opened here: CREDS is redirected to a fixture
# first and a guard asserts it.
if section("§7  subproxy — the OAuth refresh"):
    import urllib.request as _ur

    subproxy.get_access_token = _REAL_GET_TOKEN     # §3 stubbed the module
    CDIR = os.path.join(DATA, "creds")
    os.makedirs(CDIR, exist_ok=True)
    subproxy.CREDS = os.path.join(CDIR, ".credentials.json")
    assert os.path.normcase(subproxy.CREDS) != os.path.normcase(REAL_CREDS), \
        "refusing to run §7 against the host's real credentials"
    REAL_CREDS_MTIME = (os.path.getmtime(REAL_CREDS)
                        if os.path.exists(REAL_CREDS) else None)
    NOW = time.time()
    _resp = {"access_token": "AT-2", "refresh_token": "RT-2",
             "expires_in": 3600}
    _fail: list[Exception] = []
    _posts: list[dict] = []

    class _FakeHTTP:
        def __init__(self, payload):
            self.payload = json.dumps(payload).encode()

        def read(self, *a):
            return self.payload

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        _posts.append({"url": req.full_url, "method": req.get_method(),
                       "body": json.loads(req.data.decode()),
                       "headers": dict(req.header_items())})
        if _fail:
            raise _fail[0]
        return _FakeHTTP(_resp)

    _real_urlopen = _ur.urlopen
    _ur.urlopen = fake_urlopen

    def write_creds(**over):
        doc = {"claudeAiOauth": {
            "accessToken": "AT-1", "refreshToken": "RT-1",
            "expiresAt": int((time.time() + 7200) * 1000),
            "refreshTokenExpiresAt": int((time.time() + 86400 * 30) * 1000),
            "scopes": ["user:inference"], "subscriptionType": "max"},
            "organizationUuid": "org-uuid"}
        doc["claudeAiOauth"].update(over)
        with open(subproxy.CREDS, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        return doc

    def read_creds():
        return json.load(open(subproxy.CREDS, encoding="utf-8"))

    @t("available() is exactly 'is there a credentials file'")
    def _():
        if os.path.exists(subproxy.CREDS):
            os.remove(subproxy.CREDS)
        assert not subproxy.available()
        write_creds()
        assert subproxy.available()

    @t("a token with more than 5 minutes left is returned WITHOUT a refresh")
    def _():
        _posts.clear()
        assert subproxy.get_access_token() == "AT-1"
        assert _posts == [], "it refreshed a live token"

    @t("the 5-minute skew boundary is honoured in both directions")
    def _():
        _posts.clear()
        write_creds(expiresAt=int((time.time() + 301) * 1000))
        assert subproxy.get_access_token() == "AT-1" and not _posts
        write_creds(expiresAt=int((time.time() + 299) * 1000))
        assert subproxy.get_access_token() == "AT-2" and len(_posts) == 1

    @t("the refresh POST is the documented shape (grant, token, client id)")
    def _():
        p = _posts[-1]
        assert p["url"] == subproxy.TOKEN_URL and p["method"] == "POST", p
        assert p["body"] == {"grant_type": "refresh_token",
                             "refresh_token": "RT-1",
                             "client_id": subproxy.CLIENT_ID}, p["body"]
        assert p["headers"].get("Content-type") == "application/json", p

    @t("a refreshed token is written back in place, atomically")
    def _():
        write_creds(expiresAt=0)
        assert subproxy.get_access_token() == "AT-2"
        d = read_creds()["claudeAiOauth"]
        assert d["accessToken"] == "AT-2" and d["refreshToken"] == "RT-2", d
        assert abs(d["expiresAt"] / 1000 - (time.time() + 3600)) < 5, d
        assert read_creds()["organizationUuid"] == "org-uuid", \
            "the rest of the document must survive the write"
        assert not glob.glob(os.path.join(CDIR, "*.tmp")), "a temp file leaked"

    @t("an unchanged refresh token is kept (the API may omit it)")
    def _():
        global _resp
        _resp = {"access_token": "AT-3", "expires_in": 60}
        write_creds(expiresAt=0)
        assert subproxy.get_access_token() == "AT-3"
        assert read_creds()["claudeAiOauth"]["refreshToken"] == "RT-1"
        _resp = {"access_token": "AT-2", "refresh_token": "RT-2",
                 "expires_in": 3600}

    @t("☞ FIXED: a ROTATED refresh token no longer leaves a stale "
       "refreshTokenExpiresAt describing the token it replaced")
    def _():
        """Before the fix this field was never touched: after a rotation the
        file claimed an expiry belonging to a refresh token that no longer
        exists. It only ever moves further into the past, and the host CLI
        and this proxy share the one file."""
        old = write_creds(expiresAt=0)["claudeAiOauth"]["refreshTokenExpiresAt"]
        subproxy.get_access_token()
        d = read_creds()["claudeAiOauth"]
        assert d["refreshToken"] == "RT-2", d
        assert d.get("refreshTokenExpiresAt") != old, \
            "the stale expiry of the REPLACED refresh token is still there"
        fixed("subproxy.get_access_token never updated `refreshTokenExpiresAt` "
              "— a real field of ~/.claude/.credentials.json, which the host "
              "CLI and this proxy SHARE. After a refresh-token rotation the "
              "file described a token that no longer existed, drifting "
              "further into the past with every refresh. Now: recorded when "
              "the endpoint reports a lifetime, dropped when the token "
              "rotated and it does not, left alone when it did not rotate. "
              "`_write` hardened too — a failed write used to leave a stray "
              ".tmp beside the real credentials and escape as a "
              "non-RuntimeError, i.e. a bare 500 out of /anthropic.")

    @t("…and an expiry the endpoint DOES report is recorded")
    def _():
        global _resp
        _resp = {"access_token": "AT-4", "refresh_token": "RT-4",
                 "expires_in": 3600, "refresh_token_expires_in": 86400}
        try:
            write_creds(expiresAt=0)
            subproxy.get_access_token()
            d = read_creds()["claudeAiOauth"]
            assert abs(d["refreshTokenExpiresAt"] / 1000
                       - (time.time() + 86400)) < 5, d
        finally:
            _resp = {"access_token": "AT-2", "refresh_token": "RT-2",
                     "expires_in": 3600}

    @t("an UNROTATED refresh token keeps its own expiry untouched")
    def _():
        global _resp
        _resp = {"access_token": "AT-5", "expires_in": 3600}
        try:
            old = write_creds(expiresAt=0)["claudeAiOauth"][
                "refreshTokenExpiresAt"]
            subproxy.get_access_token()
            assert read_creds()["claudeAiOauth"]["refreshTokenExpiresAt"] == old
        finally:
            _resp = {"access_token": "AT-2", "refresh_token": "RT-2",
                     "expires_in": 3600}

    @t("☠ a FAILED refresh raises actionably and leaves the file untouched")
    def _():
        doc = write_creds(expiresAt=0)
        before = open(subproxy.CREDS, encoding="utf-8").read()
        _fail.append(OSError("HTTP Error 400: Bad Request"))
        try:
            subproxy.get_access_token()
            raise AssertionError("a failed refresh returned a token")
        except RuntimeError as e:
            assert "subscription token refresh failed" in str(e), e
            assert "400" in str(e), e
        finally:
            _fail.clear()
        assert open(subproxy.CREDS, encoding="utf-8").read() == before
        assert not glob.glob(os.path.join(CDIR, "*.tmp"))

    @t("a malformed refresh RESPONSE does not corrupt the credentials file")
    def _():
        global _resp
        _resp = {"nothing": "useful"}
        before = None
        try:
            write_creds(expiresAt=0)
            before = open(subproxy.CREDS, encoding="utf-8").read()
            subproxy.get_access_token()
            raise AssertionError("no raise")
        except RuntimeError as e:
            assert "refresh" in str(e).lower(), e
        finally:
            _resp = {"access_token": "AT-2", "refresh_token": "RT-2",
                     "expires_in": 3600}
        assert open(subproxy.CREDS, encoding="utf-8").read() == before, \
            "a half-applied refresh reached the shared file"

    @t("a missing credentials file is an actionable RuntimeError, not a 500")
    def _():
        os.remove(subproxy.CREDS)
        try:
            subproxy.get_access_token()
            raise AssertionError("no raise")
        except RuntimeError as e:
            assert "no readable Claude credentials" in str(e), e
            assert subproxy.CREDS in str(e), e

    @t("an unparseable credentials file says so (it does not crash the proxy)")
    def _():
        open(subproxy.CREDS, "w").write("{not json")
        try:
            subproxy.get_access_token()
            raise AssertionError("no raise")
        except RuntimeError as e:
            assert "no readable Claude credentials" in str(e), e

    @t("a file with no OAuth block tells the operator to log in")
    def _():
        json.dump({"organizationUuid": "x"},
                  open(subproxy.CREDS, "w", encoding="utf-8"))
        try:
            subproxy.get_access_token()
            raise AssertionError("no raise")
        except RuntimeError as e:
            assert "log in with the Claude Code CLI first" in str(e), e

    @t("an expired token with no refresh token asks for a re-login")
    def _():
        write_creds(expiresAt=0, refreshToken="")
        try:
            subproxy.get_access_token()
            raise AssertionError("no raise")
        except RuntimeError as e:
            assert "re-login with the CLI" in str(e), e

    @t("☞ concurrent callers refresh ONCE (the lock re-reads inside it)")
    def _():
        write_creds(expiresAt=0)
        _posts.clear()
        out, errs = [], []

        def go():
            try:
                out.append(subproxy.get_access_token())
            except Exception as e:                             # noqa: BLE001
                errs.append(e)

        ths = [threading.Thread(target=go) for _ in range(8)]
        for th in ths:
            th.start()
        for th in ths:
            th.join(20)
        assert not errs, errs
        assert out == ["AT-2"] * 8, out
        assert len(_posts) == 1, f"{len(_posts)} refreshes for one expiry"

    @t("☠ the host's REAL credentials file was never opened by this suite")
    def _():
        now = (os.path.getmtime(REAL_CREDS)
               if os.path.exists(REAL_CREDS) else None)
        assert now == REAL_CREDS_MTIME, "the real credentials file changed"
        assert not glob.glob(os.path.join(os.path.dirname(REAL_CREDS),
                                          "*.tmp"))

    _ur.urlopen = _real_urlopen


# ================================================================== §8
if section("§8  creation-time rules"):
    def create(**body):
        return call(ADMIN, "POST", "/api/orgs", body)

    @t("a sandboxed kiosk accepts any storage limit (sandboxes have no cap)")
    def _():
        r = create(name=PFX + "small1",
                   kiosk={"credits": 100, "storage_limit_mb": 256,
                          "sandbox": True})
        assert r.status == 200, r
        assert store.load_org(r.json["slug"]).d["kiosk"]["storage_limit_mb"] == 256
        drop(r.json["slug"])

    @t("an UNSANDBOXED kiosk may have any storage limit (it is a loose cap)")
    def _():
        r = create(name=PFX + "loose", kiosk={"credits": 10,
                                              "storage_limit_mb": 256,
                                              "sandbox": False})
        assert r.status == 200, r
        o = store.load_org(r.json["slug"])
        assert not sandbox.is_sandboxed(o)
        assert o.d["kiosk"]["storage_limit_mb"] == 256
        drop(o.d["slug"])

    @t("☞ a kiosk is BORN sandboxed unless the form says otherwise")
    def _():
        r = create(name=PFX + "dflt", kiosk={"credits": 10,
                                             "storage_limit_mb": 4096})
        assert r.status == 200, r
        k = store.load_org(r.json["slug"]).d["kiosk"]
        assert k["sandbox"] is True, k
        drop(r.json["slug"])

    @t("a sandboxed kiosk is minted with a 32-hex secret and warmed")
    def _():
        _warmed.clear()
        r = create(name=PFX + "mint", kiosk={"credits": 10,
                                             "storage_limit_mb": 4096,
                                             "sandbox": True})
        assert r.status == 200, r
        slug = r.json["slug"]
        k = store.load_org(slug).d["kiosk"]
        assert re.fullmatch(r"[a-f0-9]{32}", k["sandbox_secret"]), k
        assert k["sandbox_secret"] != k["token"], "one secret for two doors"
        assert _warmed == [slug], _warmed
        api._bridge_cache["at"] = 0.0
        assert api._bridge_secret_map()[k["sandbox_secret"]] == slug
        drop(slug)

    @t("a sandboxed NORMAL org gets the same isolation, no kiosk limits")
    def _():
        _warmed.clear()
        r = create(name=PFX + "normsbx", sandbox=True)
        assert r.status == 200, r
        d = store.load_org(r.json["slug"]).d
        assert d.get("kiosk") in (None, {}), d.get("kiosk")
        assert set(d["sandbox"]) == {"enabled", "secret"}, d["sandbox"]
        assert d["sandbox"]["enabled"]
        assert re.fullmatch(r"[a-f0-9]{32}", d["sandbox"]["secret"])
        assert _warmed == [r.json["slug"]]
        drop(r.json["slug"])

    @t("a plain org is never sandboxed, never warmed, and holds no secret")
    def _():
        _warmed.clear()
        r = create(name=PFX + "plain2")
        d = store.load_org(r.json["slug"]).d
        assert not d.get("sandbox") and not d.get("kiosk"), d
        assert _warmed == [], _warmed
        drop(r.json["slug"])

    @t("☠ a subscription-auth sandbox cannot be given a PUBLIC kiosk URL")
    def _():
        r = create(name=PFX + "subs", kiosk={"credits": 10,
                                             "storage_limit_mb": 4096,
                                             "sandbox": True})
        slug = r.json["slug"]
        with store.DOC_LOCK:
            o = store.load_org(slug)
            o.d["kiosk"]["api_key"] = "subscription"
            o.d["kiosk"]["enabled"] = False
            store.save_org(o)
        bad = call(ADMIN, "POST", f"/api/orgs/{slug}/kiosk", {"enabled": True})
        assert bad.status == 422 and "COPIED host credentials" in bad.text, bad
        assert not store.load_org(slug).d["kiosk"]["enabled"]
        drop(slug)

    @t("the host capability payload tells the UI what to grey out")
    def _():
        r = call(ADMIN, "GET", "/api/host")
        assert set(("docker", "sandbox_mcp")) <= set(r.json), r.json
        assert r.json["sandbox_mcp"] is False
        assert isinstance(r.json["docker"], bool)

    @t("…and the MCP list carries the same flag (servers are greyed per org)")
    def _():
        r = call(ADMIN, "GET", "/api/mcp-servers")
        assert r.json["sandbox_mcp"] is False, r.json
        os.environ["ORGTREE_SANDBOX_MCP"] = "1"
        try:
            assert call(ADMIN, "GET", "/api/mcp-servers").json["sandbox_mcp"]
        finally:
            os.environ.pop("ORGTREE_SANDBOX_MCP")

    @t("deleting an org tears its sandbox down with it")
    def _():
        r = create(name=PFX + "delsbx", sandbox=True)
        slug = r.json["slug"]
        seen = []
        real = sandbox.remove
        sandbox.remove = lambda s: seen.append(s)
        try:
            d = call(ADMIN, "DELETE", f"/api/orgs/{slug}")
            assert d.status == 200, d
        finally:
            sandbox.remove = real
        assert seen == [slug], seen

    @t("docker_available() is a cached PATH probe, never a daemon call")
    def _():
        sandbox._docker_ok = None
        assert sandbox.docker_available() is (shutil.which("docker")
                                              is not None)


# ================================================================== §9
# The real thing: a real image and a real container, checked against the
# mount contract §2 asserts on the argv. Everything created is named after a
# zzsbx- slug and removed in the finally block; nothing else on the daemon is
# inspected, stopped or deleted.
def dk(*args, timeout=300):
    # utf-8, not the console codepage: an org chart carries box-drawing
    # characters and cp1252 raised inside subprocess's reader thread
    return subprocess.run(["docker", *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


if section("§9  real Docker") and not DOCKER_TIER:
    skip("§9 needs --docker (it builds an image and runs a container; "
         "minutes, not seconds)")
elif DOCKER_TIER:
    if shutil.which("docker") is None:
        skip("§9: no docker CLI on PATH")
    elif dk("version", "--format", "{{.Server.Version}}", timeout=60
            ).returncode != 0:
        skip("§9: the Docker daemon is not running — "
             "every check below is skipped, the hermetic tiers are unaffected")
    else:
        # the hermetic tiers stubbed `_docker` — reload puts the real one
        # back IN PLACE, so every other module's reference follows
        import importlib
        importlib.reload(sandbox)
        print("       … building the sandbox image (first run: minutes)")
        TAG9 = sandbox.ensure_image()

        @t("the real image builds and carries the host CLI's version in its tag")
        def _():
            assert dk("image", "inspect", TAG9).returncode == 0
            assert supervisor.cli_version() in TAG9 or TAG9 == sandbox.IMAGE

        O9 = mkorg("real1", secret="99" * 16)
        S9 = O9.d["slug"]
        try:
            NAME9 = sandbox.ensure_container(O9)
            ins = dk("container", "inspect", "-f",
                     "{{json .Mounts}}|{{.HostConfig.ReadonlyRootfs}}|"
                     '{{index .Config.Labels "orgtree.layout"}}', NAME9)
            MOUNTS9, RO9, LAYOUT9 = ins.stdout.strip().split("|")
            BY_DST9 = {m["Destination"]: m for m in json.loads(MOUNTS9)}

            @t("the real container runs with a read-only rootfs and the layout label")
            def _():
                assert RO9 == "true", RO9
                assert LAYOUT9 == sandbox.LAYOUT, LAYOUT9

            @t("system dirs are the org's named volumes in the real container")
            def _():
                for d in sandbox.SYS_DIRS:
                    m = BY_DST9.get("/" + d)
                    assert m and m["Type"] == "volume", (d, m)
                    assert m["Name"] == sandbox.sys_volume(S9, d), m

            @t("home, workspace and scratch are host binds in the real container")
            def _():
                want = {
                    "/home/agent": sandbox.sandbox_home(S9),
                    sandbox.cpath_workspace(S9): O9.d["workspace"],
                    f"{sandbox.cpath_data()}/scratch/{S9}":
                        store.scratch_root(S9),
                }
                for dst, src in want.items():
                    m = BY_DST9.get(dst)
                    assert m and m["Type"] == "bind", (dst, m)
                    assert os.path.realpath(m["Source"]) == \
                        os.path.realpath(src), (dst, m["Source"], src)

            @t("the agent can write its workspace and scratch")
            def _():
                for d in (sandbox.cpath_workspace(S9),
                          f"{sandbox.cpath_data()}/scratch/{S9}"):
                    r = dk("exec", NAME9, "sh", "-c", f"touch {d}/.probe")
                    assert r.returncode == 0, (d, r.stderr)

            @t("⚑ DEFECT: host binds and the in-container agent disagree on uid")
            def _():
                """The image's `agent` is uid 1001; host bind sources are
                created by the backend's uid. `/home/agent` is never chowned,
                so the CLI cannot write its own home; `_heal_ownership`
                chowns the data tree to 1001, so the backend then cannot
                write workspace or scratch from the host side."""
                home = dk("exec", NAME9, "sh", "-c", "touch /home/agent/.probe")
                agent_uid = dk("exec", NAME9, "id", "-u").stdout.strip()
                if home.returncode == 0 or agent_uid == str(os.getuid()):
                    raise AssertionError("FIXED — retire this reproduction")
                note("sandbox: the image's agent uid (1001) differs from the "
                     "backend's host uid, so host bind mounts are writable "
                     "from only one side — /home/agent is read-only to the "
                     "agent, and after _heal_ownership the backend loses "
                     "write access to workspace and scratch. Needs the image "
                     "to build `agent` with the host uid (or an equivalent "
                     "mapping). Reported, not fixed.")
        finally:
            sandbox.remove(S9)
            drop(S9)

# ==========================================================================
if SKIPS:
    print("\nSKIPPED")
    for s in SKIPS:
        print(f"  ○ {s}")
if FIXED:
    print("\nFIXED HERE — each was reproduced RED before the fix")
    for i, n in enumerate(FIXED, 1):
        print(f"  ✓ {i}. {n}")
if NOTES:
    print("\nREPORTED, NOT FIXED — each has a reproduction above")
    for i, n in enumerate(NOTES, 1):
        print(f"  ⚑ {i}. {n}")
print(f"\nALL {PASS} CHECKS PASS"
      + ("" if DOCKER_TIER else "   (hermetic tier; --docker adds §9)"))
