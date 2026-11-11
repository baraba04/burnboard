#!/usr/bin/env python3
"""Command line helper of the Burnboard app: one command per run, then it exits.

  status                                   what was found, profiles, consent
  consent --limits-call on|off             store the user's consent
  profile-add --dir PATH [--name TEXT]     add a Claude config folder (must contain projects/)
  profile-use --id ID / profile-remove --id ID
  sign-in                                  run the user's own `claude auth login` for the active profile
  active-is-default                        {"default": true|false}: is the active profile the default one
  page / last / data                       the HTML page, the last snapshot, a fresh snapshot

Stdout is the response body and the exit code is 0; an error is {"error": "<code>"} with
exit code 1. Credentials are never read, stored or printed.
"""
import argparse
import json
import os
import platform
import shutil
import signal
import subprocess
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import core

NAME_MAX = 60
SIGN_IN_LIMIT = 300  # seconds; the sign-in is killed after this


class Fail(Exception):
    def __init__(self, code):
        self.code = code


class Parser(argparse.ArgumentParser):
    def error(self, message):
        print(message, file=sys.stderr)
        emit(b'{"error":"bad_arguments"}')
        sys.exit(1)


def emit(body):
    if isinstance(body, (dict, list)):
        body = json.dumps(body, separators=(",", ":"), ensure_ascii=False).encode()
    sys.stdout.buffer.write(body)
    sys.stdout.buffer.flush()


def quick(cmd, profile, timeout):
    """Start a short helper; return a function that gives its output, or None on timeout."""
    proc = core.spawn(cmd, profile)

    def result():
        try:
            return proc.communicate(timeout=timeout)[0].decode("utf-8", "replace")
        except subprocess.TimeoutExpired:
            core.end_children()
            return None
    return result


def claude_status(profile):
    found = {"found": False, "version": None, "loggedIn": None, "email": None, "subscriptionType": None}
    path = shutil.which("claude", path=core.env(profile)["PATH"])
    if not path:
        return found
    found["found"] = True
    version = quick([path, "--version"], profile, 4)
    auth = quick([path, "auth", "status", "--json"], profile, 6)
    out = version()
    if out and out.split():
        found["version"] = out.split()[0]
    try:  # keep only these three fields of the answer
        info = json.loads(auth() or "")
        if isinstance(info, dict):
            found["loggedIn"] = info["loggedIn"] if isinstance(info.get("loggedIn"), bool) else None
            for k in ("email", "subscriptionType"):
                found[k] = info[k] if isinstance(info.get(k), str) else None
    except ValueError:
        pass
    return found


def status(cfg):
    active = core.active_profile(cfg)
    c = core.consent(cfg)
    return {
        "python": platform.python_version(),
        "claude": claude_status(active),
        "profiles": [{"id": p["id"], "name": p["name"], "dir": p["dir"],
                      "exists": core.profile_dir_ok(p["dir"]), "active": p["id"] == active["id"]}
                     for p in core.profiles(cfg)],
        "sources": core.sources(active),
        "consent": c,
        "needs_setup": not c["given"],
        # the running app's own version (set by the app; the development server has none), for the More menu
        "version": os.environ.get("BURNBOARD_VERSION", "") if core.version_tuple(os.environ.get("BURNBOARD_VERSION")) else "",
    }


def cmd_status(_args):
    signal.signal(signal.SIGTERM, cut_off)  # the app stops a request it no longer needs; end the claude helpers too
    return status(core.load_config())


def cmd_consent(args):
    cfg = core.load_config()
    cfg["consent"] = {"version": core.CONSENT_VERSION, "limits_call": args.limits_call == "on",
                      "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    core.save_config(cfg)
    return status(cfg)


def cmd_profile_add(args):
    real = os.path.realpath(os.path.expanduser(args.dir))  # store the real path, never a link
    if not core.profile_dir_ok(real):
        raise Fail("bad_profile_dir")
    cfg = core.load_config()
    pid = core.profile_id(real)
    if real == os.path.realpath(core.default_profile()["dir"]) or any(p["id"] == pid for p in core.profiles(cfg)):
        raise Fail("profile_exists")
    name = "".join(ch for ch in (args.name or os.path.basename(real)) if ch.isprintable()).strip()[:NAME_MAX]
    if not name:
        raise Fail("bad_name")
    cfg["profiles"] = [{"id": p["id"], "name": p["name"], "dir": p["dir"]} for p in core.profiles(cfg)[1:]] \
        + [{"id": pid, "name": name, "dir": real}]
    core.save_config(cfg)
    return status(cfg)


def cmd_profile_use(args):
    cfg = core.load_config()
    if args.id not in [p["id"] for p in core.profiles(cfg)]:
        raise Fail("unknown_profile")
    cfg["active"] = args.id
    core.save_config(cfg)
    return status(cfg)


def cmd_profile_remove(args):
    cfg = core.load_config()
    if args.id == core.DEFAULT_ID:
        raise Fail("cannot_remove_default")
    rest = [p for p in core.profiles(cfg)[1:] if p["id"] != args.id]
    if len(rest) == len(core.profiles(cfg)) - 1:
        raise Fail("unknown_profile")
    cfg["profiles"] = rest  # its archive and limit history stay on disk
    if cfg.get("active") == args.id:
        cfg["active"] = core.DEFAULT_ID
    core.save_config(cfg)
    return status(cfg)


def cmd_active_is_default(_args):
    return {"default": core.is_default(core.active_profile(core.load_config()))}


def cmd_sign_in(_args):
    """Claude Code's own sign-in: it opens the browser and stores the result itself. Nothing it prints is
    read (stdout and stderr go to /dev/null, they can hold a sign-in URL or code), no credential passes here."""
    signal.signal(signal.SIGTERM, cut_off)
    profile = core.active_profile(core.load_config())
    path = shutil.which("claude", path=core.env(profile)["PATH"])
    if not path:
        raise Fail("no_claude")
    try:
        proc = subprocess.Popen([path, "auth", "login"], cwd=core.private_dir(profile), env=core.env(profile),
                                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                start_new_session=True)
    except OSError:
        raise Fail("sign_in_failed")
    core.CHILDREN.append(proc)
    try:
        proc.wait(timeout=SIGN_IN_LIMIT)
    except subprocess.TimeoutExpired:
        core.end_children()
        proc.wait()
        raise Fail("sign_in_failed")
    if proc.returncode != 0:
        raise Fail("sign_in_failed")
    return {"ok": True}


def cmd_page(_args):
    return core.page()


def cmd_last(_args):
    try:
        with open(core.last_path(core.active_profile(core.load_config())), "rb") as fh:
            return fh.read()
    except OSError:
        return b"{}"


def cut_off(signum, _frame):
    # Time limit or asked to stop: leave nothing behind.
    core.end_children()
    core.log("cut off by signal", signum)
    os.write(1, b'{"error":"cut_off"}')
    os._exit(1)


def cmd_data(_args):
    cfg = core.load_config()
    c = core.consent(cfg)
    if not c["given"]:
        raise Fail("setup_needed")
    profile = core.active_profile(cfg)
    if not core.is_default(profile) and not core.profile_dir_ok(profile["dir"]):
        raise Fail("profile_missing")
    signal.signal(signal.SIGALRM, cut_off)
    signal.signal(signal.SIGTERM, cut_off)
    signal.alarm(180)  # a build gets this long, but cannot hang forever
    try:
        return core.data(profile, c["limits_call"])
    except Exception as e:
        core.end_children()
        core.log("ERROR", repr(e))
        raise Fail("internal")


def build_parser():
    p = Parser(description="Burnboard helper")
    sub = p.add_subparsers(dest="command", required=True, parser_class=Parser)
    sub.add_parser("status").set_defaults(func=cmd_status)
    c = sub.add_parser("consent")
    c.add_argument("--limits-call", required=True, choices=["on", "off"])
    c.set_defaults(func=cmd_consent)
    a = sub.add_parser("profile-add")
    a.add_argument("--dir", required=True)
    a.add_argument("--name")
    a.set_defaults(func=cmd_profile_add)
    for name, func in (("profile-use", cmd_profile_use), ("profile-remove", cmd_profile_remove)):
        u = sub.add_parser(name)
        u.add_argument("--id", required=True)
        u.set_defaults(func=func)
    for name, func in (("page", cmd_page), ("last", cmd_last), ("data", cmd_data),
                       ("sign-in", cmd_sign_in), ("active-is-default", cmd_active_is_default)):
        sub.add_parser(name).set_defaults(func=func)
    return p


def main(argv=None):
    os.umask(0o077)  # everything written is readable by this user only
    args = build_parser().parse_args(argv)
    try:
        emit(args.func(args))
    except Fail as e:
        emit({"error": e.code})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
