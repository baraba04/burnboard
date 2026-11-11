#!/usr/bin/env python3
"""On-demand local server for the Burnboard dashboard.

Started by launchd (inetd style) once per HTTP request on 127.0.0.1; the
socket is stdin/stdout. Nothing stays running between requests.

  GET /            the dashboard page (page.html wrapped in a local shell)
  GET /d3.min.js   the bundled d3 (app/vendor)
  GET /last.json   the last snapshot, no work done
  GET /data.json   builds a fresh snapshot: transcripts + the public price list +
                   plan limits from one tiny `claude -p` call on the subscription
"""
import os
import signal
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import core

log = core.log


# The page may only load its own inline code and the bundled d3, and may only talk back to this server.
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


def respond(status, ctype, body):
    out = sys.stdout.buffer
    out.write((f"HTTP/1.0 {status}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
               "Cache-Control: no-store\r\nX-Content-Type-Options: nosniff\r\n"
               f"Content-Security-Policy: {CSP}\r\nX-Frame-Options: DENY\r\nReferrer-Policy: no-referrer\r\n"
               "Cross-Origin-Resource-Policy: same-origin\r\nConnection: close\r\n\r\n").encode())
    out.write(body)
    out.flush()


def trim_log(limit=1_000_000):
    # stderr is the log file launchd opened in append mode; start over when it grows large.
    try:
        if os.fstat(2).st_size > limit:
            os.ftruncate(2, 0)
    except OSError:
        pass


def main():
    signal.signal(signal.SIGALRM, core.cut_off)
    signal.signal(signal.SIGTERM, core.cut_off)
    signal.alarm(10)    # an idle or slow connection ends this process quickly
    os.umask(0o077)     # snapshot, cache and log are readable by this user only
    profile = core.active_profile(core.load_config())
    core.private_cache(profile)
    trim_log()
    rf = sys.stdin.buffer
    first = rf.readline(4096).decode("latin-1").split()
    headers = {}
    for _ in range(100):
        line = rf.readline(8192)
        if not line or line in (b"\r\n", b"\n"):
            break
        name, _, value = line.decode("latin-1").partition(":")
        headers[name.strip().lower()] = value.strip()
    else:
        return respond("431 Request Header Fields Too Large", "text/plain", b"too many headers")
    if len(first) < 2 or first[0] != "GET":
        return respond("405 Method Not Allowed", "text/plain", b"GET only")
    # Only answer requests addressed to this machine by its local name (blocks DNS rebinding).
    if headers.get("host", "").rsplit(":", 1)[0] not in ("localhost", "127.0.0.1"):
        return respond("403 Forbidden", "text/plain", b"local only")
    # Another website open in the browser must not be able to use this server.
    if headers.get("sec-fetch-site", "same-origin") not in ("same-origin", "none"):
        return respond("403 Forbidden", "text/plain", b"same origin only")
    path = first[1].split("?")[0]
    try:
        if path == "/":
            respond("200 OK", "text/html; charset=utf-8", core.page())
        elif path == "/d3.min.js":
            with open(os.path.join(core.HERE, "vendor", "d3.min.js"), "rb") as fh:
                respond("200 OK", "text/javascript; charset=utf-8", fh.read())
        elif path in ("/last.json", "/data.json") and headers.get("x-usage-claude") != "1":
            # Only our own page sends this header; a foreign page cannot add it without permission.
            respond("403 Forbidden", "application/json", b'{"error":"forbidden"}')
        elif path == "/last.json":
            last = core.last_path(profile)
            if os.path.exists(last):
                respond("200 OK", "application/json", open(last, "rb").read())
            else:
                respond("404 Not Found", "application/json", b"{}")
        elif path == "/data.json":
            signal.alarm(180)  # a build gets more time, but still cannot hang forever
            respond("200 OK", "application/json", core.data(profile))
        else:
            respond("404 Not Found", "text/plain", b"not found")
    except Exception as e:  # report instead of hanging the page
        core.end_children()
        log("ERROR", repr(e))
        respond("500 Internal Server Error", "application/json",
                b'{"error":"internal error, see serve.log"}')


if __name__ == "__main__":
    main()
