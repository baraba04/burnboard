#!/usr/bin/env python3
"""Check the app's updater (native/main.swift, "Update") and the release tool (native/release-tool.swift).

main.swift is compiled with -D UPDATE_TEST into a temporary folder: that build reads the release file URL, the allowed
zip URL prefix, the public key, the running version and the install target from environment variables and has a
window-less entry point (`--update-test`) that runs the update steps and prints the reply. Test-only switches of that
build: BURNBOARD_TEST_OWN_PATH (the path that counts as the running bundle's own place; any other target follows the
fallback rule), BURNBOARD_TEST_TRASH (a folder standing in for the Trash, so the real Trash is never touched; "fail" or
unset makes the Trash step fail) and BURNBOARD_TEST_NOSWAP (1: no exchange support, 2: also a failing second rename).
The key is a throwaway pair made by the release tool for this test, never the real one. Fixtures (release files, zips, a few bad ones) are
served from a temporary folder by a local server on 127.0.0.1; nothing leaves the machine and nothing outside the
temporary folders is touched. A plain build (no flag, -O like build-app.sh) is compiled too and must not contain the test
variable names.

Cases: valid release into an existing target (swapped, the old bundle goes to the stand-in Trash under a visible name,
nothing hidden is left) and into a new one; the Trash step failing; no exchange support (two renames, and a failing second
rename that rolls back); fallback targets (only a real Burnboard bundle is replaced; another bundle id, a file and a link
are refused untouched); leftovers of an interrupted update are removed; fixed request headers; the key in .env must match
the app's constant (check-key, app-pubkey, verify --pubkey, package.sh stopping); target that is a link (the link is moved
aside, what it pointed to is untouched); tampered zip (one byte), shorter and longer zip than signed; wrong signature; release.json edited
after signing; a signed release whose zip URL differs; downgrade; same version; zip with a link as the top-level item;
two top-level items; wrong bundle id; plist version not the signed one; unsigned bundle; bundle changed after signing;
redirect to another host; no server; 404; oversized release.json. After every case: the target is untouched or fully
replaced, no staging folder is left next to it and the temporary folder of the update is gone.
The real GitHub download, the relaunch and the quit gate are not covered here: the gate lives in the running app
(applicationShouldTerminate, called by NSApp.terminate), which the command-line entry point never starts.
Run: python3 tests/test_update.py   (needs swiftc, i.e. Xcode's command line tools)
"""
import base64
import functools
import http.server
import json
import os
import plistlib
import shutil
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import zipfile

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
MAIN, TOOL = os.path.join(ROOT, "native", "main.swift"), os.path.join(ROOT, "native", "release-tool.swift")
TMP = tempfile.TemporaryDirectory()
T = TMP.name
WWW = os.path.join(T, "www")
UPDATE_TMP = os.path.join(T, "tmpdir")          # TMPDIR of the test binary: must be empty after every update
os.makedirs(WWW)
os.makedirs(UPDATE_TMP)
CASES = {"n": 0}


def sh(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    assert r.returncode == 0, (cmd[:3], r.stdout[-400:], r.stderr[-1500:])
    return r.stdout


def compile_all():
    out = {}
    # the plain build is compiled with -O like build-app.sh does: the optimizer must not leave test names behind either
    for name, src, flags in (("tool", TOOL, ["-Onone", "-D", "UPDATE_TEST"]),
                             ("app", MAIN, ["-Onone", "-D", "UPDATE_TEST", "-framework", "Cocoa", "-framework", "WebKit"]),
                             ("plain", MAIN, ["-O", "-framework", "Cocoa", "-framework", "WebKit"])):
        out[name] = os.path.join(T, "bin-" + name)
        sh(["swiftc", "-o", out[name], src] + flags)
    return out


def make_bundle(path, version, bundle_id="app.burnboard.mac", sign=True, pad=True):
    """A small signed Burnboard.app at path (a copy of /usr/bin/true as its program, ad hoc signed)."""
    macos = os.path.join(path, "Contents", "MacOS")
    os.makedirs(macos)
    shutil.copy("/usr/bin/true", os.path.join(macos, "Burnboard"))
    with open(os.path.join(path, "Contents", "Info.plist"), "wb") as fh:
        plistlib.dump({"CFBundleIdentifier": bundle_id, "CFBundleShortVersionString": version, "CFBundleExecutable": "Burnboard",
                       "CFBundleName": "Burnboard", "CFBundlePackageType": "APPL"}, fh)
    if pad:   # the app refuses a zip under 100 KB; random bytes do not compress
        os.makedirs(os.path.join(path, "Contents", "Resources"))
        with open(os.path.join(path, "Contents", "Resources", "pad.bin"), "wb") as fh:
            fh.write(os.urandom(200 * 1024))
    if sign:
        sh(["codesign", "--force", "-s", "-", path])


def zip_dir(folder, zip_path):
    """The way package.sh zips: ditto, keeping the parent folder name."""
    sh(["/usr/bin/ditto", "-c", "-k", "--keepParent", folder, zip_path])


def tool_env(prefix, env_file):
    return dict(os.environ, BURNBOARD_ENV_FILE=env_file, BURNBOARD_TEST_ZIP_PREFIX=prefix)


class Case:
    """One scenario: a folder in the fixture server, an install target next to a victim folder."""

    def __init__(self, bins, port, pub, env_file):
        CASES["n"] += 1
        self.bins, self.port, self.pub, self.env_file = bins, port, pub, env_file
        self.name = "c%d" % CASES["n"]
        self.www = os.path.join(WWW, self.name)
        self.base = "http://127.0.0.1:%d/%s" % (port, self.name)
        self.prefix = self.base + "/v"
        self.dir = os.path.join(T, self.name)
        self.target = os.path.join(self.dir, "Applications", "Burnboard.app")
        self.trash = os.path.join(self.dir, "trash")      # stands in for the user's Trash (UPDATE_TEST build only)
        os.makedirs(self.www)
        os.makedirs(os.path.dirname(self.target))
        os.makedirs(self.trash)

    def old_target(self, version="1.2", bundle_id="app.burnboard.mac"):
        make_bundle(self.target, version, bundle_id=bundle_id)
        with open(os.path.join(self.target, "marker"), "w") as fh:   # added after signing: only to tell old from new
            fh.write("old")

    def place(self, zip_path, version="1.3"):
        """Serve a finished zip as the release zip of `version`; returns the served path."""
        served = os.path.join(self.www, "v%s" % version, "Burnboard-%s.zip" % version)
        os.makedirs(os.path.dirname(served), exist_ok=True)
        shutil.copy(zip_path, served)
        return served

    def release(self, version="1.3", folder=None, **bundle):
        """Zip a fixture bundle (or the Burnboard.app inside `folder`) as the release zip of `version`; returns its served path."""
        if folder is None:
            folder = os.path.join(self.dir, "stage")
            make_bundle(os.path.join(folder, "Burnboard.app"), bundle.pop("plist_version", version), **bundle)
        made = os.path.join(self.dir, "made.zip")
        zip_dir(os.path.join(folder, "Burnboard.app"), made)
        return self.place(made, version)

    def sign(self, zip_path, version="1.3", env_file=None, *extra):
        out = os.path.join(self.www, "release.json")
        sh([self.bins["tool"], "sign", "--zip", zip_path, "--version", version, "--out", out, *extra],
           env=tool_env(self.prefix, env_file or self.env_file))
        return out

    def update(self, running="1.2", release_url=None, own=True, trash="folder", noswap=None, expect_left=()):
        """Run the update. own: the target is the running bundle's own place (else it models a fallback target, /Applications
        or ~/Applications). trash: "folder" moves the old bundle into self.trash, "fail" makes the Trash step fail (the old
        bundle is then removed). noswap: "1" models a volume without exchange support, "2" also a failing second rename."""
        env = dict(os.environ, BURNBOARD_TEST_RELEASE_URL=release_url or self.base + "/release.json",
                   BURNBOARD_TEST_ZIP_PREFIX=self.prefix, BURNBOARD_TEST_PUBKEY=self.pub, BURNBOARD_TEST_VERSION=running,
                   BURNBOARD_TEST_TARGET=self.target, TMPDIR=UPDATE_TMP + "/",
                   BURNBOARD_TEST_TRASH=self.trash if trash == "folder" else "fail")
        if own:
            env["BURNBOARD_TEST_OWN_PATH"] = self.target
        if noswap:
            env["BURNBOARD_TEST_NOSWAP"] = noswap
        r = subprocess.run([self.bins["app"], "--update-test"], env=env, capture_output=True, text=True, timeout=180)
        assert r.returncode == 0, (r.stdout, r.stderr[-1500:])
        # the temporary folder of the update is gone, and nothing was left next to the target
        assert os.listdir(UPDATE_TMP) == [], os.listdir(UPDATE_TMP)
        left = sorted(n for n in os.listdir(os.path.dirname(self.target)) if n != "Burnboard.app")
        assert left == sorted(expect_left), left
        return json.loads(r.stdout)

    def trashed(self):
        """What the old bundle became: the names in the stand-in Trash."""
        return sorted(os.listdir(self.trash))

    def old_in_trash(self):
        """The one old bundle in the stand-in Trash, under a visible name, with its marker."""
        (name,) = self.trashed()
        assert name.startswith("Burnboard (previous ") and name.endswith(").app"), name
        assert open(os.path.join(self.trash, name, "marker")).read() == "old"
    def version(self):
        with open(os.path.join(self.target, "Contents", "Info.plist"), "rb") as fh:
            return plistlib.load(fh)["CFBundleShortVersionString"]

    def untouched(self, version="1.2"):
        assert self.version() == version and open(os.path.join(self.target, "marker")).read() == "old"

    def replaced(self, version="1.3"):
        assert self.version() == version and not os.path.exists(os.path.join(self.target, "marker"))
        sh(["codesign", "--verify", "--deep", "--strict", self.target])


class Handler(http.server.SimpleHTTPRequestHandler):
    redirects = {}   # path prefix -> URL the rest of the path is appended to
    seen = []        # (path, {header name: value}) of every request

    def do_GET(self):
        Handler.seen.append((self.path, {k: v for k, v in self.headers.items()}))
        for prefix, where in Handler.redirects.items():
            if self.path.startswith(prefix) and self.server.address_family == socket.AF_INET:   # the IPv6 server only serves
                self.send_response(302)
                self.send_header("Location", where + self.path[len(prefix):])
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        super().do_GET()

    def log_message(self, *a):
        pass


class Server6(http.server.ThreadingHTTPServer):
    address_family = socket.AF_INET6


def add_comment(zip_path):
    """Give the zip an archive comment, so a byte can be changed that no unzip step would notice."""
    data = open(zip_path, "rb").read()
    assert data[-22:-18] == b"PK\x05\x06" and data[-2:] == b"\0\0"
    open(zip_path, "wb").write(data[:-2] + struct.pack("<H", 16) + b"A" * 16)


def main():
    if not shutil.which("swiftc"):
        print("skipped: swiftc is not installed (Xcode's command line tools)")
        return
    bins = compile_all()
    # the plain build (what build-app.sh makes) has no trace of the test hooks
    strings = subprocess.run(["strings", "-a", bins["plain"]], capture_output=True, text=True).stdout
    assert "BURNBOARD_TEST_" not in strings and "--update-test" not in strings and "BURNBOARD_VERSION" in strings
    assert "BURNBOARD_TEST_" in subprocess.run(["strings", "-a", bins["app"]], capture_output=True, text=True).stdout

    keys = {}
    for name in ("good", "other"):
        env_file = os.path.join(T, name + ".env")
        pub = sh([bins["tool"], "keygen"], env=dict(os.environ, BURNBOARD_ENV_FILE=env_file)).strip()
        assert stat.S_IMODE(os.stat(env_file).st_mode) == 0o600
        keys[name] = (pub, env_file)
        assert sh([bins["tool"], "pubkey"], env=dict(os.environ, BURNBOARD_ENV_FILE=env_file)).strip() == pub
    pub, env_file = keys["good"]
    again = subprocess.run([bins["tool"], "keygen"], env=dict(os.environ, BURNBOARD_ENV_FILE=env_file), capture_output=True, text=True)
    assert again.returncode != 0 and pub not in again.stdout + again.stderr   # never overwrites, never prints a key
    assert open(env_file).read().count("BURNBOARD_UPDATE_KEY=") == 1

    # the key in .env must be the one built into the app: check-key reads the constant from main.swift by plain matching
    # (here a scratch copy of it, never the real .env or the real constant), app-pubkey prints it, verify takes --pubkey
    real_pub = sh([bins["tool"], "app-pubkey"]).strip()
    assert len(base64.b64decode(real_pub)) == 32
    text = open(MAIN).read()
    assert text.count('return "%s"' % real_pub) == 1
    scratch = os.path.join(T, "scratch-main.swift")
    open(scratch, "w").write(text.replace('return "%s"' % real_pub, 'return "%s"' % pub))
    good_env = dict(os.environ, BURNBOARD_ENV_FILE=env_file)
    assert "matches" in sh([bins["tool"], "check-key", "--main", scratch], env=good_env)
    assert sh([bins["tool"], "app-pubkey", "--main", scratch]).strip() == pub
    mismatch = subprocess.run([bins["tool"], "check-key", "--main", scratch], env=dict(os.environ, BURNBOARD_ENV_FILE=keys["other"][1]),
                              capture_output=True, text=True)
    assert mismatch.returncode != 0 and "key mismatch" in mismatch.stderr and mismatch.stdout == ""
    assert pub not in mismatch.stderr + mismatch.stdout and keys["other"][0] not in mismatch.stderr + mismatch.stdout
    open(scratch, "w").write(text.replace('    return "%s"\n' % real_pub, '    return "%s"\n    return "%s"\n' % (pub, pub)))
    assert subprocess.run([bins["tool"], "check-key", "--main", scratch], env=good_env, capture_output=True).returncode != 0   # two lines
    open(scratch, "w").write(text.replace('return "%s"' % real_pub, 'return "AAAA"'))
    assert subprocess.run([bins["tool"], "check-key", "--main", scratch], env=good_env, capture_output=True).returncode != 0   # not a key
    # package.sh with a key that is not the app's: stops before building, with the one line, and writes nothing to dist/
    dist = os.path.join(ROOT, "dist")
    before = sorted((n, os.stat(os.path.join(dist, n)).st_mtime_ns) for n in os.listdir(dist)) if os.path.isdir(dist) else None
    r = subprocess.run([os.path.join(ROOT, "native", "package.sh")], env=dict(os.environ, BURNBOARD_ENV_FILE=keys["other"][1]),
                       capture_output=True, text=True)
    assert r.returncode != 0 and "key mismatch" in r.stderr and keys["other"][0] not in r.stderr + r.stdout, (r.stdout, r.stderr)
    assert before == (sorted((n, os.stat(os.path.join(dist, n)).st_mtime_ns) for n in os.listdir(dist)) if os.path.isdir(dist) else None)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, directory=WWW))
    port = server.server_port
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # the same folder under another host (the IPv6 loopback), to be redirected to
    server6 = Server6(("::1", 0), functools.partial(Handler, directory=WWW))
    threading.Thread(target=server6.serve_forever, daemon=True).start()
    try:
        def case():
            return Case(bins, port, pub, env_file)

        # valid release: into an existing target, then into a new place; the tool's own verify agrees
        c = case(); c.old_target()
        z = c.release(); j = c.sign(z)
        sh([bins["tool"], "verify", "--json", j, "--zip", z], env=tool_env(c.prefix, env_file))
        nokey = dict(tool_env(c.prefix, os.path.join(T, "no-such.env")))      # --pubkey needs no .env at all
        assert "given key" in sh([bins["tool"], "verify", "--json", j, "--zip", z, "--pubkey", pub], env=nokey)
        wrong = subprocess.run([bins["tool"], "verify", "--json", j, "--zip", z, "--pubkey", keys["other"][0]], env=nokey,
                               capture_output=True, text=True)
        assert wrong.returncode != 0 and "signature does not match" in wrong.stderr
        Handler.seen = []
        assert c.update() == {"ok": True}
        c.replaced()
        c.old_in_trash()                      # the swap left the old bundle for the Trash, nothing hidden next to the target
        # the requests carry fixed headers and nothing about this Mac or this build (release.json and the zip)
        mine = [h for path, h in Handler.seen if path.startswith("/%s/" % c.name)]
        assert len(mine) == 2, Handler.seen
        for h in mine:
            assert h.get("User-Agent") == "Burnboard-update" and h.get("Accept-Language") == "en", h
            assert {k.lower() for k in h} <= {"host", "user-agent", "accept", "accept-language", "accept-encoding", "connection"}, h
            assert not any(w in v for v in h.values() for w in ("Darwin", "CFNetwork", "Macintosh", "Mac OS", "Burnboard/")), h
        c = case(); z = c.release(); c.sign(z)
        assert c.update() == {"ok": True}
        c.replaced()
        assert c.trashed() == []              # a new place: nothing was replaced, nothing to dispose of

        # the Trash step failing: the old bundle is removed, the new one is in place, nothing is left behind
        c = case(); c.old_target(); z = c.release(); c.sign(z)
        assert c.update(trash="fail") == {"ok": True}
        c.replaced()
        assert c.trashed() == []

        # a volume without exchange support: the two-rename way, with the same result; a failing second rename puts the
        # old bundle back, leaves nothing behind and reports the failure (the roll-back that itself fails is not modelled)
        c = case(); c.old_target(); z = c.release(); c.sign(z)
        assert c.update(noswap="1") == {"ok": True}
        c.replaced()
        c.old_in_trash()
        c = case(); z = c.release(); c.sign(z)
        assert c.update(noswap="1") == {"ok": True}
        c.replaced()
        c = case(); c.old_target(); z = c.release(); c.sign(z)
        assert c.update(noswap="2") == {"error": "update_install_failed"}
        c.untouched()
        assert c.trashed() == []

        # a fallback target (/Applications, ~/Applications: not the running bundle's own place) is replaced only when it is
        # a real Burnboard bundle folder; anything else there is refused and untouched
        c = case(); c.old_target(); z = c.release(); c.sign(z)
        assert c.update(own=False) == {"ok": True}
        c.replaced()
        c.old_in_trash()
        c = case(); c.old_target(bundle_id="app.example.other"); z = c.release(); c.sign(z)
        assert c.update(own=False) == {"error": "update_install_failed"}
        c.untouched()
        assert plistlib.load(open(os.path.join(c.target, "Contents", "Info.plist"), "rb"))["CFBundleIdentifier"] == "app.example.other"
        assert c.trashed() == []
        c = case(); z = c.release(); c.sign(z)
        open(c.target, "w").write("not a bundle")
        assert c.update(own=False) == {"error": "update_install_failed"}
        assert open(c.target).read() == "not a bundle"
        c = case(); victim = os.path.join(c.dir, "victim"); make_bundle(victim, "1.2")
        os.symlink(victim, c.target)
        z = c.release(); c.sign(z)
        assert c.update(own=False) == {"error": "update_install_failed"}
        assert os.path.islink(c.target) and os.readlink(c.target) == victim and os.path.isdir(victim)
        c = case(); z = c.release(); c.sign(z)
        assert c.update(own=False) == {"ok": True}      # nothing there yet
        c.replaced()

        # what an interrupted update left next to the target goes (only names this code makes, a link only as itself)
        import uuid
        c = case(); c.old_target(); z = c.release(); c.sign(z)
        parent = os.path.dirname(c.target)
        outside = os.path.join(c.dir, "outside"); os.makedirs(outside); open(os.path.join(outside, "keep"), "w").write("x")
        half = ".Burnboard-new-" + str(uuid.uuid4()).upper()
        os.makedirs(os.path.join(parent, half, "Contents"))
        os.symlink(outside, os.path.join(parent, ".Burnboard-new-" + str(uuid.uuid4()).upper()))
        os.makedirs(os.path.join(parent, ".Burnboard-old-" + str(uuid.uuid4()).upper()))
        os.makedirs(os.path.join(parent, ".Burnboard-new-keep"))          # not a name this code makes
        os.makedirs(os.path.join(parent, "Other.app"))
        assert c.update(expect_left=[".Burnboard-new-keep", "Other.app"]) == {"ok": True}
        c.replaced()
        assert os.listdir(outside) == ["keep"]
        c = case(); z = c.release(); c.sign(z)                           # target missing: an aside folder may be the only old copy
        aside = ".Burnboard-old-" + str(uuid.uuid4()).upper()
        os.makedirs(os.path.join(os.path.dirname(c.target), aside))
        assert c.update(expect_left=[aside]) == {"ok": True}
        c.replaced()

        # the target is a link: it is moved aside (and removed), never followed
        c = case(); victim = os.path.join(c.dir, "victim"); make_bundle(victim, "1.2")
        os.symlink(victim, c.target)
        z = c.release(); c.sign(z)
        assert c.update() == {"ok": True}
        assert not os.path.islink(c.target) and os.path.isdir(victim) and os.path.exists(os.path.join(victim, "Contents", "Info.plist"))
        c.replaced()
        (moved,) = c.trashed()                    # the link itself went to the Trash; what it pointed to stays where it was
        assert os.path.islink(os.path.join(c.trash, moved)) and os.readlink(os.path.join(c.trash, moved)) == victim
        assert plistlib.load(open(os.path.join(victim, "Contents", "Info.plist"), "rb"))["CFBundleShortVersionString"] == "1.2"

        # the zip is not what was signed: one byte changed, shorter, longer
        for how in ("flip", "flip comment", "short", "long"):
            c = case(); c.old_target(); z = c.release()
            if how == "flip comment":
                add_comment(z)          # a zip that stays valid however its last byte changes: only the checksum can tell
            c.sign(z)
            data = bytearray(open(z, "rb").read())
            if how == "flip":
                data[len(data) // 2] ^= 1
            elif how == "flip comment":
                data[-1] ^= 1
            elif how == "short":
                data = data[:-1000]
            else:
                data += b"x" * 1000
            open(z, "wb").write(bytes(data))
            assert c.update() == {"error": "update_bad_file"}, how
            c.untouched()

        # signature and release.json
        c = case(); c.old_target(); z = c.release(); c.sign(z, env_file=keys["other"][1])
        assert c.update() == {"error": "update_bad_signature"}
        c.untouched()
        c = case(); c.old_target(); z = c.release(); j = c.sign(z)
        doc = json.load(open(j)); doc["size"] = doc["size"] + 1
        json.dump(doc, open(j, "w"))
        assert c.update() == {"error": "update_bad_signature"}
        c.untouched()
        c = case(); c.old_target(); z = c.release(); j = c.sign(z)
        doc = json.load(open(j)); doc["version"] = "9.9"
        json.dump(doc, open(j, "w"))
        assert c.update() == {"error": "update_bad_signature"}
        c.untouched()
        c = case(); c.old_target(); z = c.release()
        c.sign(z, "1.3", None, "--url", "http://127.0.0.1:%d/%s/elsewhere/Burnboard-1.3.zip" % (port, c.name))   # signed, but not our URL
        assert c.update() == {"error": "update_bad_signature"}
        c.untouched()
        c = case(); c.old_target(); z = c.release(); j = c.sign(z)
        doc = json.load(open(j)); del doc["sig"]
        json.dump(doc, open(j, "w"))
        assert c.update() == {"error": "update_bad_signature"}
        c.untouched()
        c = case(); c.old_target(); z = c.release(); open(os.path.join(c.www, "release.json"), "w").write("[1, 2")
        assert c.update() == {"error": "update_bad_signature"}
        c.untouched()

        # versions: downgrade and same version are signed fine and refused
        c = case(); c.old_target("1.4"); z = c.release(); c.sign(z)
        assert c.update(running="1.4") == {"error": "update_none"}
        c.untouched("1.4")
        c = case(); c.old_target("1.3"); z = c.release(); c.sign(z)
        assert c.update(running="1.3") == {"error": "update_none"}
        assert c.update(running="1.3.0") == {"error": "update_none"}
        assert c.update(running="1.5") == {"error": "update_none"}
        assert c.update(running="") == {"error": "update_none"}
        c.untouched("1.3")
        c = case(); c.old_target(); z = c.release(version="1.2.1"); c.sign(z, "1.2.1")
        assert c.update(running="1.2") == {"ok": True}      # a patch number counts as newer
        c.replaced("1.2.1")

        # bundles that must be refused, all validly signed and hashed
        def refuse(label, make):
            c = case(); c.old_target()
            z = make(c); c.sign(z)
            assert c.update() == {"error": "update_bad_file"}, label
            c.untouched()
        refuse("wrong bundle id", lambda c: c.release(bundle_id="app.example.other"))
        refuse("plist version differs from the signed one", lambda c: c.release(plist_version="1.2.9"))
        refuse("not code signed", lambda c: c.release(sign=False))

        def changed_after_signing(c):
            src = os.path.join(c.dir, "stage")
            make_bundle(os.path.join(src, "Burnboard.app"), "1.3")
            with open(os.path.join(src, "Burnboard.app", "Contents", "Resources", "pad.bin"), "r+b") as fh:
                fh.write(b"changed after signing")
            return c.release(folder=src)
        refuse("bundle changed after code signing", changed_after_signing)

        def two_items(c):
            src = os.path.join(c.dir, "stage")
            make_bundle(os.path.join(src, "Burnboard.app"), "1.3")   # a perfect bundle, plus one more item beside it
            with open(os.path.join(src, "README.txt"), "w") as fh:
                fh.write("x")
            z = os.path.join(c.dir, "two.zip")
            sh(["/usr/bin/ditto", "-c", "-k", src, z])
            return c.place(z)
        refuse("two top-level items", two_items)

        def link_on_top(c):
            real = os.path.join(c.dir, "elsewhere", "Burnboard.app")
            make_bundle(real, "1.3")                           # a perfect bundle, reached through a link
            z = os.path.join(c.dir, "link.zip")
            with zipfile.ZipFile(z, "w") as zf:
                info = zipfile.ZipInfo("Burnboard.app")
                info.create_system = 3
                info.external_attr = (stat.S_IFLNK | 0o755) << 16
                info.extra = struct.pack("<HH", 0x9999, 65531) + b"\0" * 65531   # padding: the app refuses a zip under 100 KB
                zf.comment = b"\0" * 65535
                zf.writestr(info, real)
            return c.place(z)
        refuse("link as the top-level item", link_on_top)

        # download problems: another host after a redirect, nothing listening, a missing file, a huge release.json
        Handler.redirects = {}
        c = case(); c.old_target(); z = c.release(); c.sign(z)
        Handler.redirects["/%s/v1.3/" % c.name] = "http://[::1]:%d/%s/v1.3/" % (server6.server_port, c.name)
        assert c.update() == {"error": "update_no_network"}
        c.untouched()
        Handler.redirects = {}
        c = case(); c.old_target(); z = c.release(); c.sign(z)
        assert c.update(release_url="http://127.0.0.1:1/release.json") == {"error": "update_no_network"}
        assert c.update(release_url=c.base + "/missing.json") == {"error": "update_no_network"}
        os.remove(z)
        assert c.update() == {"error": "update_no_network"}                                  # release file fine, zip gone (404)
        open(os.path.join(c.www, "release.json"), "w").write('{"pad": "' + "x" * 70000 + '"}')
        assert c.update() == {"error": "update_no_network"}
        c.untouched()
    finally:
        for srv in (server, server6):
            srv.shutdown()
            srv.server_close()
    print("ok: updater and release tool (valid release into an existing, a new and a linked target; tampered, short and long zip; "
          "wrong signature, edited, unsigned and malformed release.json; foreign zip URL; downgrade and same version; wrong bundle id, "
          "wrong plist version, unsigned and altered bundle, two top-level items, link at top level; foreign redirect, no server, 404, "
          "oversized release.json; swap, Trash failure, no exchange support and its roll-back, fallback-target rule, leftovers, fixed headers, "
          "key-match check and package.sh stop; keygen never overwrites; the -O plain build has no test hooks). "
          "Not covered: the real GitHub download, relaunch, a target on another volume, a read-only target")


if __name__ == "__main__":
    main()
    TMP.cleanup()
