"""Shared logic of the dashboard: serve.py (development server) and cli.py (helper of the
app) both call these functions.

Profile: one Claude config folder (default ~/.claude, or $CLAUDE_CONFIG_DIR when set). The
default profile keeps its data where it always was; any other profile gets profiles/<id>/
under both the data folder and the cache folder. The data and cache folders can be moved
with USAGE_CLAUDE_DATA and USAGE_CLAUDE_CACHE (the tests do this); the other places that are read can be
moved with USAGE_CLAUDE_COWORK and USAGE_CLAUDE_XDG.
"""
import fcntl
import glob
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
HOME = os.path.expanduser("~")
HISTORY_DAYS = 15  # how much limit history the page receives
CONSENT_VERSION = 2  # 2: Cowork sessions of the Claude desktop app are read too. Raise when the app starts doing something the user has not agreed to
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


def sources(profile):
    """What is there to read, by counting files without opening them: [{kind, found, files}]."""
    import build as B
    return [{"kind": kind, "found": os.path.isdir(top), "files": sum(1 for _ in B.jsonl_files(top))}
            for kind, top in read_roots(profile)]


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
    prices, offline = pricing.load(cache_root())  # the one request besides the limits call; shared by all profiles
    if offline:
        notes.append({"code": "prices_offline"})
    if lim:
        limits = read_limits(lim)
        if limits.get("error"):
            notes.append({"code": "limits_failed", "why": limits["error"]})
    else:
        limits = {"windows": []}
        notes.append({"code": "limits_off"})
    roots, groups = naming(load_config())
    srcs = read_roots(profile)
    out = B.build(prices, limits, os.path.join(cache_dir(profile), "parsed.pickle"),
                  os.path.join(data_dir(profile), "archive.json"), log=log,
                  root=srcs[0][1], project_roots=roots, group_folders=groups, extra=srcs[1:])
    out["limit_history_cols"] = ["t", "key", "pct", "resetsAt"]
    out["limit_history"] = record_limits(profile, limits, out["meta"][0]["generated_at"])
    chk = out["meta"][0]["check"]
    if chk["unpriced_models"]:
        notes.append({"code": "unpriced", "detail": ", ".join(chk["unpriced_models"])})
    if chk["approx_pricing"]:
        notes.append({"code": "approx_pricing", "count": chk["approx_pricing"], "detail": str(chk["approx_pricing"])})
    out["meta"][0]["notes"] = notes
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
            return open(last, "rb").read()  # another request just built it
        return fresh(profile, limits_call)


def page():
    shell = open(os.path.join(HERE, "local-shell.html"), encoding="utf-8").read()
    body = open(os.path.join(HERE, "page.html"), encoding="utf-8").read()
    return shell.replace("<!--PAGE-->", body).encode()
