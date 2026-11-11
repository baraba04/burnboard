"""Shared logic of the dashboard: serve.py (development server) and cli.py (helper of the
app) both call these functions.

Profile: one Claude config folder (default ~/.claude, or $CLAUDE_CONFIG_DIR when set). The
default profile keeps its data where it always was; any other profile gets profiles/<id>/
under both the data folder and the cache folder. The data and cache folders can be moved
with USAGE_CLAUDE_DATA and USAGE_CLAUDE_CACHE (the tests do this); the other places that are read can be
moved with USAGE_CLAUDE_COWORK, USAGE_CLAUDE_XDG and USAGE_CLAUDE_PLAN_HISTORY.
"""
import fcntl
import glob
import hashlib
import json
import math
import os
import re
import shutil
import signal
import ssl
import stat
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
HOME = os.path.expanduser("~")
HISTORY_DAYS = 15  # how much limit history the page receives
PLAN_HISTORY_MAX = 1 << 20  # bytes; a larger plan usage history file is not read
RELEASE_URL = "https://raw.githubusercontent.com/baraba04/burnboard/main/release.json"
RELEASE_MAX_BYTES = 64 * 1024
CONSENT_VERSION = 4  # 2: Cowork sessions of the Claude desktop app are read too; 3: and its plan usage history file; 4: a request for the release file at every refresh and the Update button's download. Raise when the app starts doing something the user has not agreed to
DEFAULT_ID = "default"
ID_RE = re.compile(r"^[0-9a-f]{8}$")


def log(*a):
    print(datetime.now().strftime("%F %T"), *a, file=sys.stderr, flush=True)


# --- folders, config, profiles --------------------------------------------------------

def data_root():
    # Kept for good (not a cache): archive of past days, limit history and the config.
    return os.environ.get("USAGE_CLAUDE_DATA") or os.path.join(HOME, "Library", "Application Support", "usage-claude")


def cache_root():
    # Outside ~/.claude so backups skip it.
    return os.environ.get("USAGE_CLAUDE_CACHE") or os.path.join(HOME, "Library", "Caches", "claude-usage-dashboard")


def cowork_root():
    # Cowork sessions of the Claude desktop app; read for the default profile only.
    return os.environ.get("USAGE_CLAUDE_COWORK") or os.path.join(
        HOME, "Library", "Application Support", "Claude", "local-agent-mode-sessions")


def xdg_root():
    # The other default place of Claude Code logs; read for the default profile only.
    return os.environ.get("USAGE_CLAUDE_XDG") or os.path.join(HOME, ".config", "claude", "projects")


def plan_history_path():
    # Plan usage history kept by the Claude desktop app; read for the default profile only.
    return os.environ.get("USAGE_CLAUDE_PLAN_HISTORY") or os.path.join(
        HOME, "Library", "Application Support", "Claude", "plan-usage-history.json")


def config_path():
    return os.path.join(data_root(), "config.json")


def load_config():
    """The config as a dict; a missing or broken file is an empty config."""
    try:
        with open(config_path()) as fh:
            cfg = json.load(fh)
    except (OSError, ValueError):
        return {}
    return cfg if isinstance(cfg, dict) else {}


def save_config(cfg):
    """Write atomically, mode 0600, in a 0700 folder."""
    d = data_root()
    os.makedirs(d, mode=0o700, exist_ok=True)
    tmp = f"{config_path()}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as fh:
            json.dump(cfg, fh, indent=1, ensure_ascii=False)
    except BaseException:
        os.unlink(tmp)
        raise
    os.replace(tmp, config_path())


def consent(cfg):
    """{given, version, limits_call}; a stored consent of an older version is not given."""
    c = cfg.get("consent")
    if not isinstance(c, dict):
        return {"given": False, "version": None, "limits_call": None}
    v, lim = c.get("version"), c.get("limits_call")
    v = v if type(v) is int else None
    lim = lim if isinstance(lim, bool) else None
    return {"given": v is not None and v >= CONSENT_VERSION and lim is not None, "version": v, "limits_call": lim}


# --- update check -----------------------------------------------------------------------

def version_tuple(v):
    """(major, minor, patch) of "1.3" or "1.3.1" (digits only, 20 characters at most), else None."""
    if not isinstance(v, str) or len(v) > 20 or not re.fullmatch(r"[0-9]+(\.[0-9]+){1,2}", v, re.ASCII):
        return None
    t = tuple(int(x) for x in v.split("."))
    return t + (0,) * (3 - len(t))


def fetch_release(url=RELEASE_URL):
    """The parsed release.json; raises on any failure. Same care as the price list fetch (pricing.fetch): one
    host, redirects only within it, a time and a size limit, certificates verified. A plain GET with one fixed
    User-Agent, the same one the app's own update download sends (nothing about this Mac or this build)."""
    import pricing
    ctx = ssl.create_default_context()
    if not ctx.get_ca_certs() and os.path.exists(pricing.SYSTEM_CA):
        ctx.load_verify_locations(pricing.SYSTEM_CA)  # a python.org build has no trust store of its own; verification stays on
    opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx), pricing._SameHost())
    req = urllib.request.Request(url, headers={"User-Agent": "Burnboard-update"})
    with opener.open(req, timeout=pricing.TIMEOUT_S) as r:
        body = r.read(RELEASE_MAX_BYTES + 1)
    if len(body) > RELEASE_MAX_BYTES:
        raise ValueError("release file too large")
    return json.loads(body)


def update_check():
    """{"version": "x.y"} when the published release is newer than the running app (BURNBOARD_VERSION, set by the
    app; the development server has none, so no request), else None. Silent on any failure. Only the version
    leaves this function: the page never sees anything else from the file, and the app fetches and checks the
    file itself when the user clicks Update."""
    running = version_tuple(os.environ.get("BURNBOARD_VERSION"))
    if running is None or os.environ.get("USAGE_CLAUDE_OFFLINE") == "1":
        return None
    try:
        version = fetch_release().get("version")
    except Exception:  # network, certificate, timeout, size, bad JSON, not an object: no update shown
        return None
    latest = version_tuple(version)
    return {"version": version} if latest and latest > running else None


def current_update(body):
    """A stored snapshot as it may be served now: its update notice is kept only when it names a version newer than
    the running app. A snapshot written by an older version just before it updated itself must not offer that update."""
    if b'"update"' not in body:
        return body
    try:
        snap = json.loads(body)
        meta = snap["meta"][0]
        found = meta.get("update")
    except (ValueError, KeyError, IndexError, TypeError, AttributeError):
        return body
    if found is None:
        return body
    running = version_tuple(os.environ.get("BURNBOARD_VERSION"))
    latest = version_tuple(found.get("version")) if isinstance(found, dict) else None
    if running and latest and latest > running:
        return body
    del meta["update"]
    return json.dumps(snap, separators=(",", ":"), ensure_ascii=False).encode()


def default_profile():
    return {"id": DEFAULT_ID, "name": "Default",
            "dir": os.path.expanduser(os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude"))}


def is_default(profile):
    return profile["id"] == DEFAULT_ID


def profile_dir_ok(path):
    return os.path.isdir(path) and os.path.isdir(os.path.join(path, "projects"))


def profile_id(real_dir):
    return hashlib.sha256(real_dir.encode()).hexdigest()[:8]


def profiles(cfg):
    """The default profile, then the added ones (ids that are not 8 hex digits are ignored:
    the id becomes a folder name)."""
    out, seen = [default_profile()], {DEFAULT_ID}
    for p in cfg.get("profiles") if isinstance(cfg.get("profiles"), list) else []:
        if (isinstance(p, dict) and all(isinstance(p.get(k), str) for k in ("id", "name", "dir"))
                and ID_RE.match(p["id"]) and p["id"] not in seen):
            seen.add(p["id"])
            out.append({"id": p["id"], "name": p["name"], "dir": p["dir"]})
    return out


def active_profile(cfg):
    for p in profiles(cfg):
        if p["id"] == cfg.get("active"):
            return p
    return default_profile()


def read_roots(profile):
    """[(kind, folder)] the logs are read from: the profile's own projects folder first; the default profile
    also reads the other default place of Claude Code and the Cowork sessions of the Claude desktop app."""
    out = [("claude_code", os.path.join(profile["dir"], "projects"))]
    if is_default(profile):
        if os.path.realpath(xdg_root()) != os.path.realpath(out[0][1]):
            out.append(("claude_code_xdg", xdg_root()))
        out.append(("cowork", cowork_root()))
    return out


def plan_history():
    """[(unix seconds, five_hour %, seven_day %)] of the last HISTORY_DAYS from the Claude desktop app's plan usage
    history, oldest first. Read-only: a regular file only (a link is not followed), at most PLAN_HISTORY_MAX bytes.
    Any other shape or type gives no samples, never an exception. The org id in the file is never looked at."""
    try:
        fd = os.open(plan_history_path(), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return []
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return []
        with os.fdopen(fd, "rb", closefd=False) as fh:
            raw = fh.read(PLAN_HISTORY_MAX + 1)
        if len(raw) > PLAN_HISTORY_MAX:
            return []
        doc = json.loads(raw.decode("utf-8"))
    except (OSError, ValueError, RecursionError):
        return []
    finally:
        os.close(fd)
    items = doc.get("samples") if isinstance(doc, dict) else None
    if not isinstance(items, list):
        return []

    def num(x):
        return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)
    now = time.time()
    cutoff = now - HISTORY_DAYS * 86400
    out = []
    for s in items:
        u = s.get("u") if isinstance(s, dict) else None
        if not isinstance(u, dict) or not (num(s.get("t")) and num(u.get("fh")) and num(u.get("sd"))):
            continue
        if cutoff <= s["t"] / 1000 <= now:
            out.append((s["t"] / 1000, round(u["fh"]), round(u["sd"])))
    return sorted(out)


def sources(profile):
    """What is there to read: [{kind, found, files}], the log folders by counting files without opening them. The
    default profile also lists the plan usage history (files: 0 or 1, samples: readings of the last HISTORY_DAYS)."""
    import build as B
    out = [{"kind": kind, "found": os.path.isdir(top), "files": sum(1 for _ in B.jsonl_files(top))}
           for kind, top in read_roots(profile)]
    if is_default(profile):
        n = len(plan_history())
        out.append({"kind": "plan_history", "found": n > 0, "files": 1 if n else 0, "samples": n})
    return out


def naming(cfg):
    """(project_roots, group_folders) for build.project_of."""
    def names(key):
        v = cfg.get(key)
        return [x for x in v if isinstance(x, str) and x] if isinstance(v, list) else []
    return names("project_roots"), names("group_folders")


def cache_dir(profile):
    return cache_root() if is_default(profile) else os.path.join(cache_root(), "profiles", profile["id"])


def data_dir(profile):
    return data_root() if is_default(profile) else os.path.join(data_root(), "profiles", profile["id"])


def private_cache(profile):
    d = cache_dir(profile)
    os.makedirs(d, mode=0o700, exist_ok=True)
    os.chmod(d, 0o700)
    return d


def private_dir(profile):
    d = os.path.join(cache_dir(profile), "probe")
    os.makedirs(d, mode=0o700, exist_ok=True)
    return d


# --- helper processes -----------------------------------------------------------------

def env(profile=None):
    e = dict(os.environ)
    node_bins = sorted(glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin")))
    e["PATH"] = ":".join(node_bins[::-1] + ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin",
                                            os.path.expanduser("~/.local/bin")])  # where the native installer puts claude
    e.setdefault("HOME", HOME)
    if profile and not is_default(profile):
        e["CLAUDE_CONFIG_DIR"] = profile["dir"]  # read by claude
    return e


CHILDREN = []


def spawn(cmd, profile, child_env=None):
    # Each helper gets its own process group so it and anything it starts can be ended together.
    p = subprocess.Popen(cmd, cwd=private_dir(profile), env=child_env or env(profile), stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, start_new_session=True)
    CHILDREN.append(p)
    return p


def end_children(*_):
    for p in CHILDREN:
        if p.poll() is None:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass


def cut_off(signum, _frame):
    # Timer ran out or we were asked to stop: leave nothing behind.
    end_children()
    log("cut off by signal", signum)
    os._exit(1)


def limits_env(profile):
    """The environment of the limits call: no ANTHROPIC_* variable (API key, auth token, base URL, extra headers)
    and no cloud-provider switch, so the call goes to Anthropic on the user's subscription and is never billed to
    a key or sent to a host this process happened to inherit."""
    return {k: v for k, v in env(profile).items()
            if not k.startswith("ANTHROPIC_") and not k.startswith("CLAUDE_CODE_USE_")}


def start_limits(profile):
    # One minimal Haiku call; its stream reports the plan's rate-limit windows. The flags keep the context it
    # sends small: measured 11 Nov 2026 about 830 input tokens, and none of --safe-mode,
    # CLAUDE_CODE_DISABLE_CLAUDE_MDS or CLAUDE_CODE_DISABLE_AUTO_MEMORY made that smaller.
    # Without Claude Code on this Mac (a person who only uses Cowork) there is no call: None.
    if not shutil.which("claude", path=env(profile)["PATH"]):
        return None
    cmd = ["claude", "-p", "Reply with the single word OK.", "--model", "haiku",
           "--output-format", "stream-json", "--verbose", "--no-session-persistence",
           "--strict-mcp-config", "--disable-slash-commands", "--tools", "",
           "--system-prompt", "Reply with OK.", "--setting-sources", ""]
    return spawn(cmd, profile, limits_env(profile))


def read_limits(proc):
    keys = ("five_hour", "seven_day")  # the page translates these
    try:
        out, _ = proc.communicate(timeout=60)
    except subprocess.TimeoutExpired:
        end_children()
        return {"windows": [], "error": "timeout"}
    for line in out.decode("utf-8", "replace").splitlines():
        try:
            o = json.loads(line)
        except ValueError:
            continue
        if o.get("type") != "rate_limit_event":
            continue
        wins = (o.get("rate_limit_info") or {}).get("unifiedWindows") or {}
        res = []
        for key in keys:
            w = wins.get(key)
            if w and w.get("utilization") is not None and w.get("resetsAt"):
                res.append({"key": key, "percentUsed": round(w["utilization"] * 100),
                            "resetsAt": datetime.fromtimestamp(w["resetsAt"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")})
        if res:
            return {"plan": "Max", "windows": res}
    return {"windows": [], "error": "nodata"}


def record_limits(profile, limits, now):
    """Append this reading to the history and return the recent rows for the page."""
    d = data_dir(profile)
    history = os.path.join(d, "limit-history.jsonl")
    os.makedirs(d, mode=0o700, exist_ok=True)
    if limits.get("windows"):
        with open(history, "a") as fh:
            fh.write(json.dumps({"t": now, "w": {w["key"]: [w["percentUsed"], w["resetsAt"]]
                                                 for w in limits["windows"]}}) + "\n")
    rows, cutoff = [], time.time() - HISTORY_DAYS * 86400
    try:
        with open(history) as fh:
            for line in fh:
                try:
                    o = json.loads(line)
                    if datetime.fromisoformat(o["t"].replace("Z", "+00:00")).timestamp() < cutoff:
                        continue
                    for key, (pct, reset) in o["w"].items():
                        rows.append([o["t"], key, pct, reset])
                except (ValueError, KeyError, TypeError):
                    continue
    except OSError:
        pass
    return rows


# --- snapshot and page ------------------------------------------------------------------

def last_path(profile):
    return os.path.join(cache_dir(profile), "last.json")


def fresh(profile, limits_call=True):
    import build as B
    import pricing
    t0 = time.time()
    lim = start_limits(profile) if limits_call else None
    notes = []
    prices, offline = pricing.load(cache_root())  # a request besides the limits call; shared by all profiles
    if offline:
        notes.append({"code": "prices_offline"})
    update = update_check()  # the other request: the release file
    if lim:
        limits = read_limits(lim)
        if limits.get("error"):
            notes.append({"code": "limits_failed", "why": limits["error"]})
    else:
        limits = {"windows": []}
        if not limits_call:
            notes.append({"code": "limits_off"})  # without Claude Code there is simply no call: no note, nothing to fix
    roots, groups = naming(load_config())
    srcs = read_roots(profile)
    out = B.build(prices, limits, os.path.join(cache_dir(profile), "parsed.pickle"),
                  os.path.join(data_dir(profile), "archive.json"), log=log,
                  root=srcs[0][1], project_roots=roots, group_folders=groups, extra=srcs[1:])
    out["limit_history_cols"] = ["t", "key", "pct", "resetsAt"]
    out["limit_history"] = record_limits(profile, limits, out["meta"][0]["generated_at"])
    # What the Claude desktop app recorded: no reset times, so kept apart from limit_history. Shown in place of the
    # limits when the limits call gave none.
    app = plan_history() if is_default(profile) else []
    iso = lambda ts: datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    out["app_limit_history_cols"] = ["t", "key", "pct"]
    out["app_limit_history"] = [[iso(ts), key, pct] for ts, fh, sd in app for key, pct in (("five_hour", fh), ("seven_day", sd))]
    if app and not limits.get("windows"):
        out["meta"][0]["app_limits"] = {"t": iso(app[-1][0]), "five_hour": app[-1][1], "seven_day": app[-1][2]}
    chk = out["meta"][0]["check"]
    if chk["unpriced_models"]:
        notes.append({"code": "unpriced", "detail": ", ".join(chk["unpriced_models"])})
    if chk["approx_pricing"]:
        notes.append({"code": "approx_pricing", "count": chk["approx_pricing"], "detail": str(chk["approx_pricing"])})
    out["meta"][0]["notes"] = notes
    if update:
        out["meta"][0]["update"] = update
    out["meta"][0]["took_s"] = round(time.time() - t0, 1)
    body = json.dumps(out, separators=(",", ":"), ensure_ascii=False).encode()
    last = last_path(profile)
    tmp = last + ".tmp"
    with open(tmp, "wb") as fh:
        fh.write(body)
    os.replace(tmp, last)
    log(f"fresh snapshot in {out['meta'][0]['took_s']} s, notes={notes}")
    return body


def data(profile, limits_call=True):
    last = last_path(profile)
    with open(os.path.join(private_cache(profile), "lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # one build at a time
        if os.path.exists(last) and time.time() - os.path.getmtime(last) < 10:
            return current_update(open(last, "rb").read())  # another request just built it
        return fresh(profile, limits_call)


def page():
    shell = open(os.path.join(HERE, "local-shell.html"), encoding="utf-8").read()
    body = open(os.path.join(HERE, "page.html"), encoding="utf-8").read()
    return shell.replace("<!--PAGE-->", body).encode()
