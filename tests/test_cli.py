#!/usr/bin/env python3
"""Check config, consent, profiles, project naming and the data command of app/cli.py.

Everything runs in temporary folders (USAGE_CLAUDE_DATA / USAGE_CLAUDE_CACHE); the real
config, archive and cache are never touched, no helper program is started for real and
nothing is fetched (USAGE_CLAUDE_OFFLINE=1; the price list is a stub where a test needs one).
The sign-in command is tested against a stand-in `claude` script on a PATH that cannot reach the real one.
Run: python3 tests/test_cli.py
"""
import io
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import redirect_stdout

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, APP)

TMP = tempfile.TemporaryDirectory()
os.environ["USAGE_CLAUDE_DATA"] = os.path.join(TMP.name, "data")
os.environ["USAGE_CLAUDE_CACHE"] = os.path.join(TMP.name, "cache")
os.environ["USAGE_CLAUDE_OFFLINE"] = "1"
os.environ["USAGE_CLAUDE_COWORK"] = os.path.join(TMP.name, "no-cowork")  # never the real Cowork or XDG folders
os.environ["USAGE_CLAUDE_XDG"] = os.path.join(TMP.name, "no-xdg")
os.environ.pop("CLAUDE_CONFIG_DIR", None)
import build as B  # noqa: E402
import cli  # noqa: E402
import core  # noqa: E402
import pricing  # noqa: E402

CONFIG = os.path.join(TMP.name, "data", "config.json")
REAL_START_LIMITS = core.start_limits  # test_data_with_consent replaces it with a stand-in


def run(*argv):
    """Run a cli command in this process; return (exit code, parsed output)."""
    buf = io.StringIO()
    buf.buffer = io.BytesIO()
    real = sys.stdout
    sys.stdout = buf
    try:
        try:
            code = cli.main(list(argv))
        except SystemExit as e:  # argparse errors end the process
            code = e.code
    finally:
        sys.stdout = real
    return code, json.loads(buf.buffer.getvalue())


def make_profile(name):
    d = os.path.join(TMP.name, name)
    os.makedirs(os.path.join(d, "projects", "-x"))
    return d


def reset():
    for p in (CONFIG,):
        if os.path.exists(p):
            os.remove(p)


def test_config_and_consent():
    reset()
    assert core.load_config() == {}                      # missing file
    c = core.consent({})
    assert c == {"given": False, "version": None, "limits_call": None}
    core.save_config({"extra": 1})
    assert stat.S_IMODE(os.stat(CONFIG).st_mode) == 0o600
    assert stat.S_IMODE(os.stat(os.path.dirname(CONFIG)).st_mode) == 0o700
    assert [f for f in os.listdir(os.path.dirname(CONFIG)) if f.endswith(".tmp")] == []
    assert core.load_config() == {"extra": 1}            # round trip
    with open(CONFIG, "w") as fh:
        fh.write("{broken")
    assert core.load_config() == {}                      # broken file: nothing given
    with open(CONFIG, "w") as fh:
        fh.write("[1]")
    assert core.load_config() == {}
    reset()

    code, out = run("status")
    assert code == 0 and out["needs_setup"] is True and out["consent"]["given"] is False
    assert [p["id"] for p in out["profiles"]] == ["default"] and out["profiles"][0]["active"] is True
    assert set(out["claude"]) == {"found", "version", "loggedIn", "email", "subscriptionType"}
    assert "node" not in out and isinstance(out["python"], str)

    code, out = run("consent", "--limits-call", "off")
    assert code == 0 and out["consent"] == {"given": True, "version": core.CONSENT_VERSION, "limits_call": False}
    assert out["needs_setup"] is False
    stored = json.load(open(CONFIG))["consent"]
    assert stored["version"] == core.CONSENT_VERSION and stored["limits_call"] is False and stored["at"].endswith("Z")
    assert stat.S_IMODE(os.stat(CONFIG).st_mode) == 0o600

    # a stored consent of an older version counts as not given; a newer one does
    cfg = json.load(open(CONFIG))
    cfg["consent"]["version"] = core.CONSENT_VERSION - 1
    assert core.consent(cfg)["given"] is False and core.consent(cfg)["limits_call"] is False
    cfg["consent"]["version"] = core.CONSENT_VERSION + 1
    assert core.consent(cfg)["given"] is True
    cfg["consent"] = {"version": 1, "limits_call": True}       # version 1 (before Cowork was read) is not valid
    assert core.CONSENT_VERSION == 2 and core.consent(cfg)["given"] is False
    cfg["consent"] = {"version": "1", "limits_call": True}
    assert core.consent(cfg)["given"] is False
    cfg["consent"] = {"version": core.CONSENT_VERSION}
    assert core.consent(cfg)["given"] is False

    code, out = run("consent", "--limits-call", "maybe")
    assert code == 1 and out == {"error": "bad_arguments"}
    reset()


def usage_line(mid, rid, out, cwd="/sandbox/x", ts="2026-11-10T10:00:00.000Z", **extra):
    o = {"type": "assistant", "timestamp": ts, "uuid": "u" + mid, "cwd": cwd,
         "message": {"id": mid, "model": "claude-sonnet-5-5", "content": [],
                     "usage": {"input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 100,
                               "cache_creation_input_tokens": 20}}}
    if rid:
        o["requestId"] = rid
    o.update(extra)
    return json.dumps(o) + "\n"


def write(path, *lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        fh.writelines(lines)


def test_other_sources():
    reset()
    stub = pricing.sanitize({"claude-sonnet-5-5": {
        "input_cost_per_token": 1e-5, "output_cost_per_token": 5e-5, "cache_creation_input_token_cost": 1.25e-5,
        "cache_read_input_token_cost": 1e-6}})
    base = os.path.join(TMP.name, "src")
    home_profile = os.path.join(base, "claude-home")      # stands in for ~/.claude
    cowork, xdg, outside = (os.path.join(base, n) for n in ("cowork", "xdg", "outside"))
    local = os.path.join(cowork, "a1", "a2", "local_abc123")
    audit = os.path.join(local, "audit.jsonl")
    copy = os.path.join(local, ".claude", "projects", "-sessions-sandbox", "sess1.jsonl")
    write(os.path.join(home_profile, "projects", "-w-app", "s.jsonl"), usage_line("m0", "r0", 5, cwd="/w/app"))
    # the same call in both Cowork spots: the audit copy has no requestId and no timestamp, only _audit_timestamp
    audit_line = json.loads(usage_line("m1", None, 6, ts="x"))
    del audit_line["timestamp"], audit_line["cwd"]
    audit_line["_audit_timestamp"] = "2026-11-10T10:00:01.000Z"
    write(audit, json.dumps(audit_line) + "\n", json.dumps(dict(audit_line, _audit_timestamp="2026-11-10T10:00:04.000Z")) + "\n")
    write(copy, usage_line("m1", "r1", 6), usage_line("m1", "r1", 40, ts="2026-11-10T10:00:04.000Z"),
          usage_line("m2", "r2", 90, ts="2026-11-10T10:00:20.000Z"))
    time.sleep(0.05)
    os.utime(copy)                                         # the audit file is the older one
    write(os.path.join(xdg, "-w-xdg-app", "x.jsonl"), usage_line("m3", "r3", 7, cwd="/w/xdg-app"))
    write(os.path.join(outside, "leak.jsonl"), usage_line("m9", "r9", 1000))
    os.symlink(outside, os.path.join(local, "link-dir"))   # a link inside the Cowork tree that points out of it
    os.symlink(os.path.join(outside, "leak.jsonl"), os.path.join(local, "link-file.jsonl"))
    default = dict(core.default_profile(), dir=home_profile)
    os.environ["USAGE_CLAUDE_COWORK"], os.environ["USAGE_CLAUDE_XDG"] = cowork, xdg
    try:
        srcs = core.read_roots(default)
        assert [k for k, _ in srcs] == ["claude_code", "claude_code_xdg", "cowork"]
        out = B.build(stub, {"windows": []}, None, None, root=srcs[0][1], extra=srcs[1:])
        by_project = {}
        for r in out["facts"]:
            by_project.setdefault(r[2], [0, 0])
            by_project[r[2]][0] += r[B.NV + 1]               # output tokens
            by_project[r[2]][1] += r[B.NV + 6]               # calls
        # Cowork: m1 once (highest output of its copies, 40) and m2; the sandbox cwd does not name the project
        assert by_project == {"/w/app": [5, 1], "/w/xdg-app": [7, 1], "Cowork": [130, 2]}, by_project
        assert 1000 not in [r[B.NV + 1] for r in out["facts"]]  # nothing behind the links was read
        cw = [i for i, s in enumerate(out["sessions"]) if s[2] == "Cowork"]
        assert len(cw) == 1 and out["sessions"][cw[0]][0] == "abc123" and out["sessions"][cw[0]][5] == 2, out["sessions"]
        # the same build twice with a cache gives the same totals (the parse cache holds the Cowork files)
        cache = os.path.join(TMP.name, "src-cache", "parsed.pickle")
        a = B.build(stub, {"windows": []}, cache, None, root=srcs[0][1], extra=srcs[1:])
        b = B.build(stub, {"windows": []}, cache, None, root=srcs[0][1], extra=srcs[1:])
        assert a["facts"] == b["facts"] == out["facts"] and b["meta"][0]["check"]["files_parsed"] == 0

        # status.sources: counts only, links not counted, nothing read; a missing folder is found: false
        assert core.sources(default) == [{"kind": "claude_code", "found": True, "files": 1},
                                         {"kind": "claude_code_xdg", "found": True, "files": 1},
                                         {"kind": "cowork", "found": True, "files": 2}]
        os.environ["USAGE_CLAUDE_XDG"] = os.path.join(base, "none")
        assert core.sources(default)[1] == {"kind": "claude_code_xdg", "found": False, "files": 0}
        assert [k for k, _ in core.read_roots(default)] == ["claude_code", "claude_code_xdg", "cowork"]
        os.environ["USAGE_CLAUDE_XDG"] = xdg

        # a non-default profile reads neither of them
        other = {"id": "0123abcd", "name": "x", "dir": os.path.join(base, "claude-home")}
        assert core.read_roots(other) == [("claude_code", os.path.join(home_profile, "projects"))]
        assert core.sources(other) == [{"kind": "claude_code", "found": True, "files": 1}]
        out2 = B.build(stub, {"windows": []}, None, None, root=core.read_roots(other)[0][1])
        assert [r[2] for r in out2["facts"]] == ["/w/app"]
    finally:
        os.environ["USAGE_CLAUDE_COWORK"] = os.path.join(TMP.name, "no-cowork")
        os.environ["USAGE_CLAUDE_XDG"] = os.path.join(TMP.name, "no-xdg")

    # through the command: status of the default profile lists the three sources; the other folders can be absent
    cfg_home = os.environ.pop("CLAUDE_CONFIG_DIR", None)
    os.environ["CLAUDE_CONFIG_DIR"] = home_profile
    os.environ["USAGE_CLAUDE_COWORK"], os.environ["USAGE_CLAUDE_XDG"] = cowork, xdg
    try:
        code, st = run("status")
        assert code == 0 and [(s["kind"], s["found"], s["files"]) for s in st["sources"]] == [
            ("claude_code", True, 1), ("claude_code_xdg", True, 1), ("cowork", True, 2)], st["sources"]
        assert all(set(s) == {"kind", "found", "files"} for s in st["sources"])
    finally:
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        if cfg_home:
            os.environ["CLAUDE_CONFIG_DIR"] = cfg_home
        os.environ["USAGE_CLAUDE_COWORK"] = os.path.join(TMP.name, "no-cowork")
        os.environ["USAGE_CLAUDE_XDG"] = os.path.join(TMP.name, "no-xdg")
    reset()


def test_profiles():
    reset()
    prof = make_profile("prof-a")
    real = os.path.realpath(prof)
    code, out = run("profile-add", "--dir", prof, "--name", "Work")
    assert code == 0, out
    added = [p for p in out["profiles"] if p["id"] != "default"]
    assert len(added) == 1 and added[0]["name"] == "Work" and added[0]["dir"] == real and added[0]["exists"]
    assert len(added[0]["id"]) == 8 and out["profiles"][0]["active"] is True
    pid = added[0]["id"]
    assert core.profile_id(real) == pid                    # stable

    # same folder again, also through a symlink: one profile only
    link = os.path.join(TMP.name, "link-a")
    os.symlink(prof, link)
    code, out = run("profile-add", "--dir", link)
    assert code == 1 and out == {"error": "profile_exists"}
    # a symlink to a valid folder is accepted under its real path
    prof_b = make_profile("prof-b")
    link_b = os.path.join(TMP.name, "link-b")
    os.symlink(prof_b, link_b)
    code, out = run("profile-add", "--dir", link_b)
    assert code == 0 and out["profiles"][-1]["dir"] == os.path.realpath(prof_b) and out["profiles"][-1]["name"] == "prof-b"
    bid = out["profiles"][-1]["id"]

    # rejected: no projects/, a file, a missing folder, a link to a folder without projects/, a dangling link,
    # a projects entry that is a link to a file
    nop = os.path.join(TMP.name, "no-projects")
    os.makedirs(nop)
    afile = os.path.join(TMP.name, "afile")
    open(afile, "w").close()
    link_nop = os.path.join(TMP.name, "link-nop")
    os.symlink(nop, link_nop)
    dangling = os.path.join(TMP.name, "dangling")
    os.symlink(os.path.join(TMP.name, "nowhere"), dangling)
    badproj = os.path.join(TMP.name, "badproj")
    os.makedirs(badproj)
    os.symlink(afile, os.path.join(badproj, "projects"))
    for bad in (nop, afile, os.path.join(TMP.name, "missing"), link_nop, dangling, badproj):
        code, out = run("profile-add", "--dir", bad)
        assert code == 1 and out == {"error": "bad_profile_dir"}, (bad, out)
    # the default folder itself is not added a second time
    prof_c = make_profile("prof-c")
    os.environ["CLAUDE_CONFIG_DIR"] = prof_c               # the default profile points here now
    try:
        assert core.default_profile()["dir"] == prof_c
        code, out = run("profile-add", "--dir", prof_c)
        assert code == 1 and out == {"error": "profile_exists"}
    finally:
        del os.environ["CLAUDE_CONFIG_DIR"]
    assert len(json.load(open(CONFIG))["profiles"]) == 2

    code, out = run("profile-use", "--id", "nope")
    assert code == 1 and out == {"error": "unknown_profile"}
    code, out = run("profile-use", "--id", pid)
    assert code == 0 and [p["id"] for p in out["profiles"] if p["active"]] == [pid]
    assert core.active_profile(core.load_config())["dir"] == real

    code, out = run("profile-remove", "--id", "default")
    assert code == 1 and out == {"error": "cannot_remove_default"}
    code, out = run("profile-remove", "--id", "zzzzzzzz")
    assert code == 1 and out == {"error": "unknown_profile"}
    code, out = run("profile-remove", "--id", pid)         # removing the active one falls back to default
    assert code == 0 and [p["id"] for p in out["profiles"]] == ["default", bid]
    assert out["profiles"][0]["active"] is True

    # an id that is not 8 hex digits in the config would become a folder name: ignored
    cfg = json.load(open(CONFIG))
    cfg["profiles"].append({"id": "../evil", "name": "x", "dir": prof})
    cfg["active"] = "../evil"
    assert [p["id"] for p in core.profiles(cfg)] == ["default", bid]
    assert core.active_profile(cfg)["id"] == "default"
    reset()


def test_paths_and_env():
    d, p = core.default_profile(), {"id": "0123abcd", "name": "x", "dir": "/some/where"}
    assert core.data_dir(d) == os.environ["USAGE_CLAUDE_DATA"] and core.cache_dir(d) == os.environ["USAGE_CLAUDE_CACHE"]
    assert core.data_dir(p) == os.path.join(os.environ["USAGE_CLAUDE_DATA"], "profiles", "0123abcd")
    assert core.cache_dir(p) == os.path.join(os.environ["USAGE_CLAUDE_CACHE"], "profiles", "0123abcd")
    assert core.env(p)["CLAUDE_CONFIG_DIR"] == "/some/where"
    os.environ.pop("CLAUDE_CONFIG_DIR", None)
    assert "CLAUDE_CONFIG_DIR" not in core.env(d)


def test_project_naming():
    home = B.HOME
    roots, groups = ["/w/root"], ["client", "mcp"]
    f = lambda cwd, r=roots, g=groups: B.project_of(cwd, "fold", r, g)
    assert f("/w/root") == "root (supervisor)"
    assert f("/w/root/app") == "app" and f("/w/root/app/sub/deep") == "app"
    assert f("/w/root/client/shop") == "client/shop" and f("/w/root/client/shop/src") == "client/shop"
    assert f("/w/root/client") == "client"                 # a group folder alone is just itself
    assert f("/w/rootless/x") == "/w/rootless/x"           # prefix of the name is not the folder
    assert f(home + "/play/x") == "~/play/x" and f("/elsewhere") == "/elsewhere"
    assert f(None) == "fold" and f("") == "fold"
    # without roots in the config every cwd falls to the path relative to home
    assert B.project_of("/w/root/app", "fold") == "/w/root/app"
    assert B.project_of(home + "/work", "fold") == "~/work"
    assert B.project_of(home + "/work/client/shop", "fold") == "~/work/client/shop"
    # a root given with ~ and a trailing slash
    assert B.project_of(home + "/work/a", "fold", ["~/work/"], []) == "a"
    cfg = {"project_roots": ["/w/root", 5, ""], "group_folders": ["client", None]}
    assert core.naming(cfg) == (["/w/root"], ["client"]) and core.naming({}) == ([], [])


def test_data_without_consent():
    reset()
    env = dict(os.environ, USAGE_CLAUDE_CACHE=os.path.join(TMP.name, "untouched-cache"))
    r = subprocess.run([sys.executable, os.path.join(APP, "cli.py"), "data"], env=env, capture_output=True, timeout=30)
    assert r.returncode == 1 and json.loads(r.stdout) == {"error": "setup_needed"}, (r.stdout, r.stderr)
    assert not os.path.exists(env["USAGE_CLAUDE_CACHE"]), "data without consent must not do any work"
    # an old consent version is refused too
    core.save_config({"consent": {"version": core.CONSENT_VERSION - 1, "limits_call": True}})
    r = subprocess.run([sys.executable, os.path.join(APP, "cli.py"), "data"], env=env, capture_output=True, timeout=30)
    assert r.returncode == 1 and json.loads(r.stdout) == {"error": "setup_needed"}
    reset()


def fake_proc(profile, text):
    return core.spawn(["/bin/echo", text], profile)


def test_data_with_consent():
    reset()
    prof = make_profile("prof-d")
    cwd_dir = os.path.join(prof, "projects", "-w-root-app")
    line = {"type": "assistant", "timestamp": "2026-11-10T10:00:00.000Z", "uuid": "u1", "requestId": "r1",
            "cwd": "/w/root/app", "message": {"id": "m1", "model": "claude-sonnet-5-5", "content": [],
                                              "usage": {"input_tokens": 10, "output_tokens": 5,
                                                        "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20}}}
    os.makedirs(cwd_dir, exist_ok=True)
    with open(os.path.join(cwd_dir, "s.jsonl"), "w") as fh:
        fh.write(json.dumps(line) + "\n")
    code, out = run("profile-add", "--dir", prof)
    pid = out["profiles"][-1]["id"]
    run("profile-use", "--id", pid)
    cfg = core.load_config()
    cfg["project_roots"] = ["/w/root"]
    core.save_config(cfg)
    run("consent", "--limits-call", "off")

    calls = {"prices": 0}
    stub = pricing.sanitize({"claude-sonnet-5-5": {
        "input_cost_per_token": 1e-5, "output_cost_per_token": 5e-5, "cache_creation_input_token_cost": 1.25e-5,
        "cache_read_input_token_cost": 1e-6}})
    real_load = pricing.load

    def fake_load(cache_dir, *a):
        calls["prices"] += 1
        assert cache_dir == core.cache_root()               # one stored list for all profiles
        return stub, False

    def no_limits(profile):
        raise AssertionError("the limits call must not run when switched off")

    assert not hasattr(core, "start_ccusage")               # nothing in the refresh path runs npx
    core.start_limits, pricing.load = no_limits, fake_load
    code, out = run("data")
    assert code == 0, out
    notes = out["meta"][0]["notes"]
    assert {"code": "limits_off"} in notes and not any(n["code"] in ("limits_failed", "prices_offline", "unpriced",
                                                                       "approx_pricing") for n in notes), notes
    assert out["meta"][0]["check"]["unpriced_models"] == [] and out["meta"][0]["check"]["approx_pricing"] == 0
    assert abs(out["facts"][0][B.NV + 4] - (10 * 1e-5 + 5 * 5e-5 + 20 * 1.25e-5 + 100 * 1e-6)) < 1e-4
    assert out["limits"] == [] and out["limit_history"] == []
    assert [r[2] for r in out["facts"]] == ["app"], out["facts"]   # project from the root in the config
    # a non-default profile keeps everything under profiles/<id>/, nothing in the default places
    assert os.path.exists(os.path.join(core.cache_root(), "profiles", pid, "last.json"))
    assert os.path.exists(os.path.join(core.cache_root(), "profiles", pid, "parsed.pickle"))
    assert os.path.exists(os.path.join(core.data_root(), "profiles", pid, "archive.json"))
    assert not os.path.exists(os.path.join(core.cache_root(), "last.json"))
    assert not os.path.exists(os.path.join(core.data_root(), "archive.json"))
    code, last = run("last")
    assert code == 0 and last["meta"] == out["meta"]
    code, out2 = run("data")                                 # within 10 s: reused, no second build
    assert code == 0 and calls["prices"] == 1

    # offline (the price list could not be fetched): the shipped list prices the call and the page gets the note
    pricing.load = real_load
    os.remove(os.path.join(core.cache_root(), "profiles", pid, "last.json"))
    code, out3 = run("data")
    assert code == 0 and {"code": "prices_offline"} in out3["meta"][0]["notes"] and out3["meta"][0]["check"]["unpriced_models"] == []
    assert out3["facts"][0][B.NV + 4] > 0
    pricing.load = fake_load

    # a call with a speed ccusage does not know (it drops the line) is priced at the computed price and counted in a note
    fast = dict(line, uuid="u2", requestId="r2", message=dict(line["message"], id="m2", usage=dict(
        line["message"]["usage"], speed="turbo")))
    fast_path = os.path.join(cwd_dir, "fast.jsonl")
    with open(fast_path, "w") as fh:
        fh.write(json.dumps(fast) + "\n")
    os.remove(os.path.join(core.cache_root(), "profiles", pid, "last.json"))
    code, out4 = run("data")
    assert code == 0 and {"code": "approx_pricing", "count": 1, "detail": "1"} in out4["meta"][0]["notes"], out4["meta"][0]["notes"]
    assert sum(r[-1] for r in out4["facts"]) == 2
    os.remove(fast_path)
    os.remove(os.path.join(core.cache_root(), "profiles", pid, "last.json"))
    code, out4 = run("data")
    assert code == 0 and not any(n["code"] == "approx_pricing" for n in out4["meta"][0]["notes"])

    # `compare` is not part of the app any more (tests/compare_ccusage.py): the command is unknown
    code, out = run("compare")
    assert code == 1 and out == {"error": "bad_arguments"}, out
    # the developer script: no npx is an error code; with a stand-in for ccusage it reports the cells it matched
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import compare_ccusage
    real_which, real_spawn = compare_ccusage.shutil.which, core.spawn
    compare_ccusage.shutil.which = lambda name, path=None: None
    try:
        assert compare_ccusage.compare() == {"error": "no_node"}
    finally:
        compare_ccusage.shutil.which = real_which
    day = B.parse_ts("2026-11-10T10:00:00Z").astimezone().strftime("%Y-%m-%d")
    own = 10 * 1e-5 + 5 * 5e-5 + 20 * 1.25e-5 + 100 * 1e-6
    ccusage_json = json.dumps({"daily": [{"period": day, "modelBreakdowns": [{"modelName": "claude-sonnet-5-5", "cost": own}]}]})
    seen_cmd = []

    def fake_npx(cmd, profile):
        seen_cmd.append(cmd)
        return real_spawn(["/bin/echo", ccusage_json], profile)

    compare_ccusage.shutil.which = lambda name, path=None: "/usr/bin/npx"
    core.spawn = fake_npx
    try:
        cmp_out = compare_ccusage.compare()
    finally:
        compare_ccusage.shutil.which, core.spawn = real_which, real_spawn
    assert seen_cmd == [["npx", "-y", "ccusage@20.0.48", "daily", "--breakdown", "--json"]], seen_cmd
    assert cmp_out["cells"] == 1 and cmp_out["within_cent"] == 1 and cmp_out["max_abs_diff"] < 1e-3, cmp_out
    assert set(cmp_out) == {"cells", "within_cent", "max_abs_diff", "total_ccusage", "total_own", "us_geo_calls"}
    assert cmp_out["us_geo_calls"] == 0

    # limits on: the call runs for this profile and the reading lands in this profile's history
    run("consent", "--limits-call", "on")
    os.remove(os.path.join(core.cache_root(), "profiles", pid, "last.json"))
    reading = {"type": "rate_limit_event", "rate_limit_info": {"unifiedWindows": {
        "five_hour": {"utilization": 0.25, "resetsAt": 1900000000}}}}
    core.start_limits = lambda profile: fake_proc(profile, json.dumps(reading))
    pricing.load = real_load
    code, out = run("data")
    assert code == 0 and out["limits"][0]["percentUsed"] == 25 and not any(n["code"].startswith("limits") for n in out["meta"][0]["notes"])
    assert os.path.exists(os.path.join(core.data_root(), "profiles", pid, "limit-history.jsonl"))
    assert not os.path.exists(os.path.join(core.data_root(), "limit-history.jsonl"))

    # a profile folder that vanished is reported, not built
    run("profile-use", "--id", pid)
    os.rename(prof, prof + ".gone")
    code, out = run("data")
    assert code == 1 and out == {"error": "profile_missing"}
    os.rename(prof + ".gone", prof)
    reset()

# Stand-in for the user's `claude`: notes how it was started, prints a sign-in-looking secret to both
# outputs (the app must not read it) and then behaves as FAKE_CLAUDE_MODE says.
FAKE_CLAUDE = """#!/bin/bash
echo "$* cfg=${CLAUDE_CONFIG_DIR-unset} cwd=$PWD" > "$FAKE_CLAUDE_LOG"
echo "https://example.invalid/sign-in?code=SECRET-SENTINEL"
echo "SECRET-SENTINEL" >&2
case "$FAKE_CLAUDE_MODE" in
  ok) cat > /dev/null; exit 0 ;;
  fail) exit 1 ;;
  sleep) sleep 60 & echo $! > "$FAKE_CLAUDE_LOG.pid"; wait ;;
esac
"""
FAKE_BIN = os.path.join(TMP.name, "fakebin")
FAKE_PATH = FAKE_BIN + ":/usr/bin:/bin"   # the real claude (wherever it is installed) is not on it
FAKE_LOG = os.path.join(TMP.name, "fake-claude.log")


# Stand-in `claude` that records the names of the environment variables it was started with, one file per command
# (`--version` and `auth status` start at the same time).
ENV_CLAUDE = """#!/bin/bash
env | cut -d= -f1 | sort > "$FAKE_CLAUDE_LOG.$1"
case "$1" in
  -p) echo '{"type":"rate_limit_event","rate_limit_info":{"unifiedWindows":{"five_hour":{"utilization":0.5,"resetsAt":1900000000}}}}' ;;
  auth) echo '{"loggedIn":true}' ;;
  *) echo "1.0.0" ;;
esac
"""
SECRETS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_ANYTHING")


def test_limits_env():
    """The limits call runs without an API key, auth token or cloud-provider switch; the sign-in state and sign-in
    commands keep the environment as it is."""
    reset()
    bin_dir, log = os.path.join(TMP.name, "envbin"), os.path.join(TMP.name, "env-claude.log")
    os.makedirs(bin_dir)
    fake = os.path.join(bin_dir, "claude")
    with open(fake, "w") as fh:
        fh.write(ENV_CLAUDE)
    os.chmod(fake, 0o755)
    saved = {k: os.environ.get(k) for k in SECRETS + ("FAKE_CLAUDE_LOG", "CLAUDE_CODE_OTHER")}
    real_env = core.env
    os.environ.update({k: "x" for k in SECRETS}, FAKE_CLAUDE_LOG=log, CLAUDE_CODE_OTHER="keep")
    core.env = lambda profile=None: dict(real_env(profile), PATH=bin_dir + ":/usr/bin:/bin")
    try:
        profile = core.default_profile()
        env = core.limits_env(profile)
        assert not any(k in env for k in SECRETS) and env["CLAUDE_CODE_OTHER"] == "keep" and "PATH" in env and "HOME" in env
        assert all(k in core.env(profile) for k in SECRETS)           # the general environment is not changed
        limits = core.read_limits(REAL_START_LIMITS(profile))
        assert limits["windows"][0]["percentUsed"] == 50, limits
        cli.claude_status(profile)                                    # --version and auth status
        started = {c: set(open(log + "." + c).read().split()) for c in ("-p", "--version", "auth")}
        assert not any(k in started["-p"] for k in SECRETS), started["-p"]
        assert "CLAUDE_CODE_OTHER" in started["-p"] and "HOME" in started["-p"]
        assert all(k in started["auth"] and k in started["--version"] for k in SECRETS)   # the user's own environment
    finally:
        core.env = real_env
        for k, v in saved.items():
            os.environ.pop(k, None)
            if v is not None:
                os.environ[k] = v


def pid_gone(pid):
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.1)
    return False


def test_sign_in():
    reset()
    os.makedirs(FAKE_BIN)
    fake = os.path.join(FAKE_BIN, "claude")
    with open(fake, "w") as fh:
        fh.write(FAKE_CLAUDE)
    os.chmod(fake, 0o755)
    os.environ["FAKE_CLAUDE_LOG"] = FAKE_LOG
    real_env, real_limit = core.env, cli.SIGN_IN_LIMIT

    def fake_env(profile=None):
        e = real_env(profile)
        e["PATH"] = FAKE_PATH
        return e

    core.env = fake_env
    try:
        assert cli.shutil.which("claude", path=core.env()["PATH"]) == fake   # never the real program

        # success: the process exits 0; stdout and stderr go nowhere (not into this process's outputs either)
        os.environ["FAKE_CLAUDE_MODE"] = "ok"
        cli.SIGN_IN_LIMIT = 20
        keep = os.path.join(TMP.name, "fd-capture")
        saved = os.dup(1), os.dup(2)
        with open(keep, "wb") as cap:
            os.dup2(cap.fileno(), 1)
            os.dup2(cap.fileno(), 2)
            try:
                code, out = run("sign-in")
            finally:
                os.dup2(saved[0], 1)
                os.dup2(saved[1], 2)
                os.close(saved[0])
                os.close(saved[1])
        assert code == 0 and out == {"ok": True}, (code, out)
        assert b"SECRET-SENTINEL" not in open(keep, "rb").read()
        note = open(FAKE_LOG).read()
        assert note.startswith("auth login cfg=unset cwd=") and "probe" in note, note   # default profile, own folder

        # quick failure
        os.environ["FAKE_CLAUDE_MODE"] = "fail"
        code, out = run("sign-in")
        assert code == 1 and out == {"error": "sign_in_failed"}, (code, out)

        # a non-default profile signs in to its own folder
        prof = make_profile("prof-signin")
        run("profile-add", "--dir", prof)
        pid = core.profiles(core.load_config())[-1]["id"]
        assert run("active-is-default")[1] == {"default": True}
        run("profile-use", "--id", pid)
        assert run("active-is-default")[1] == {"default": False}
        os.environ["FAKE_CLAUDE_MODE"] = "ok"
        assert run("sign-in") == (0, {"ok": True})
        assert open(FAKE_LOG).read().startswith("auth login cfg=" + os.path.realpath(prof) + " "), open(FAKE_LOG).read()
        run("profile-use", "--id", "default")

        # past the time limit: killed with everything it started
        os.environ["FAKE_CLAUDE_MODE"] = "sleep"
        cli.SIGN_IN_LIMIT = 1
        t0 = time.time()
        code, out = run("sign-in")
        assert code == 1 and out == {"error": "sign_in_failed"} and time.time() - t0 < 10, (code, out)
        assert pid_gone(int(open(FAKE_LOG + ".pid").read()))
        cli.SIGN_IN_LIMIT = real_limit

        # asked to stop (the app quits mid sign-in): it ends with the process it started
        os.remove(FAKE_LOG + ".pid")
        script = ("import sys; sys.path.insert(0, %r); import core, cli; real = core.env\n"
                  "def e(p=None):\n    x = real(p); x['PATH'] = %r; return x\n"
                  "core.env = e; sys.exit(cli.main(['sign-in']))" % (APP, FAKE_PATH))
        proc = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        for _ in range(100):
            if os.path.exists(FAKE_LOG + ".pid") and open(FAKE_LOG + ".pid").read().strip():
                break
            time.sleep(0.1)
        child = int(open(FAKE_LOG + ".pid").read())
        proc.terminate()
        assert proc.wait(timeout=10) == 1 and json.loads(proc.stdout.read()) == {"error": "cut_off"}
        assert pid_gone(child)

        # the CLI is not found
        os.makedirs(os.path.join(TMP.name, "empty-bin"))
        os.environ["FAKE_CLAUDE_MODE"] = "ok"
        core.env = lambda profile=None: dict(real_env(profile), PATH=os.path.join(TMP.name, "empty-bin"))
        assert run("sign-in") == (1, {"error": "no_claude"})
        # and the limits call is not started at all (a person with only Cowork sessions still gets a dashboard)
        assert REAL_START_LIMITS(None) is None
    finally:
        core.env, cli.SIGN_IN_LIMIT = real_env, real_limit
        reset()


def main():
    for t in (test_config_and_consent, test_other_sources, test_profiles, test_paths_and_env, test_project_naming,
              test_data_without_consent, test_data_with_consent, test_limits_env, test_sign_in):
        t()
    print("ok: config round trip and mode, consent versions, profiles (add/use/remove, bad folders, links), "
          "Cowork and ~/.config/claude read for the default profile only (one call counted once, links not followed, source counts), "
          "project naming, data refused without consent, data with limits off and on, limits call without API key variables, sign-in (success, failure, other profile, time limit, SIGTERM, no CLI)")


if __name__ == "__main__":
    main()
    TMP.cleanup()
