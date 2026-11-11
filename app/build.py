#!/usr/bin/env python3
"""Build the snapshot for the Burnboard dashboard.

Reads Claude Code transcripts under a profile's projects folder (default ~/.claude/projects, read-only), aggregates
token usage per hour, model, project, agent type, effort and the skill or MCP
server a call ran under. Cost is priced per call
with pricing.py (the rules are in research/pricing/FINDINGS.md); a message written
several times while it streamed counts once: its time is the first line's, its
tokens are the last (highest output) line's.

Past days are also kept in an archive, so history survives when Claude Code
deletes old transcripts.

CLI:  build.py <prices.json> <limits.json> <out.json> [cache.pickle] [archive.json]
Module: build(prices, limits, cache_path=None, archive_path=None, log=None, root=ROOT,
        project_roots=(), group_folders=()) -> dict
"""
import bisect
import heapq
import json
import math
import os
import pickle
import re
import shutil
import sys
from collections import defaultdict
from datetime import datetime, timezone

import pricing

HOME = os.path.expanduser("~")
ROOT = os.path.join(HOME, ".claude", "projects")  # default; a profile passes its own
SESSION_HOURS_WINDOW = 72  # hours of per-session hourly detail kept
CACHE_VERSION = 8
FACT_COLS = ["hour", "model", "project", "sub", "agent", "effort", "via",
             "input", "output", "cache_write", "cache_read", "cost", "saved", "msgs"]
NV = 7                    # index of the first value column in a fact row
TOP_CALLS_PER_DAY = 20    # costliest single calls kept per local day
CTX_POINTS = 150          # points of a session's context-size series
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-")
STATUS = re.compile(r"\b([45]\d\d)\b")
EVENT_MARKS = ('"compact_boundary"', '"api_error"', '"isApiErrorMessage":true')


def project_of(cwd, folder, project_roots=(), group_folders=()):
    """Name of the project a call ran in. Under a project root (from the config) it is the first
    folder below the root, or the first two when the first is a group folder; the root itself is
    "<root name> (supervisor)". Anything else is the path relative to home."""
    if cwd:
        for root in project_roots:
            root = os.path.expanduser(root).rstrip("/")
            if not root:
                continue
            if cwd == root:
                return os.path.basename(root) + " (supervisor)"
            if cwd.startswith(root + "/"):
                parts = cwd[len(root) + 1:].split("/")
                if parts[0] in group_folders and len(parts) > 1:
                    return "/".join(parts[:2])
                return parts[0]
        if cwd.startswith(HOME):
            return "~" + cwd[len(HOME):]
        return cwd
    return folder


def parse_ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def agent_of(path, is_sub_file):
    """'main' for a session's own transcript, else the subagent type from its meta file."""
    if not is_sub_file:
        return "main"
    try:
        with open(path[:-6] + ".meta.json") as fh:
            return str(json.load(fh).get("agentType") or "unknown")[:60]
    except (OSError, ValueError, AttributeError, RecursionError):  # unreadable, not JSON, not an object, too deep
        return "unknown"


def norm_ts(ts):
    try:  # one malformed timestamp must not break every later build
        return parse_ts(ts).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except (ValueError, TypeError, AttributeError):
        return None


def via_of(o):
    """The MCP server or skill a call ran under, as 'mcp:<name>' / 'skill:<name>', else ''."""
    mcp, skill = o.get("attributionMcpServer"), o.get("attributionSkill")
    if isinstance(mcp, str) and mcp:
        return "mcp:" + mcp[:60]
    if isinstance(skill, str) and skill:
        return "skill:" + skill[:60]
    return ""


def event_of(o):
    """Return (event, done) for a compaction or API error line, else (None, False).
    event = ("c", uuid, ts, trigger, pre_tokens, post_tokens, cwd) or ("e", uuid, ts, kind, cwd)."""
    kind, sub = o.get("type"), o.get("subtype")
    ts = norm_ts(o.get("timestamp"))
    if not ts:
        return None, False
    uuid = o.get("uuid")
    if not isinstance(uuid, str):
        uuid = None
    cwd = o.get("cwd")
    if not isinstance(cwd, str):
        cwd = None
    if kind == "system" and sub == "compact_boundary":
        m = o.get("compactMetadata")
        if not isinstance(m, dict):
            m = {}
        num = lambda v: v if isinstance(v, int) else 0
        return ("c", uuid, ts, str(m.get("trigger") or "")[:20],
                num(m.get("preTokens")), num(m.get("postTokens")), cwd), True
    if kind == "system" and sub == "api_error":
        e = o.get("error")
        code = "other"
        if isinstance(e, dict):
            if e.get("isNetworkDown"):
                code = "network"
            elif e.get("noResponse"):
                code = "no_response"
            else:
                m = STATUS.search(str(e.get("formatted") or e.get("message") or ""))
                code = m.group(1) if m else "other"
        return ("e", uuid, ts, "retry:" + code, cwd), True
    if kind == "assistant" and o.get("isApiErrorMessage") is True:
        err = o.get("error")
        return ("e", uuid, ts, "final:" + (err[:30] if isinstance(err, str) and err else "other"),
                cwd), False
    return None, False


def count_of(v):
    """A token count: None is 0, an int >= 0 is itself; anything else raises, and the line is skipped."""
    if v is None:
        return 0
    if isinstance(v, bool) or not isinstance(v, int) or v < 0:
        raise ValueError("not a token count")
    return v


def parse_file(path, is_sub_file):
    """Return (title, agent, entries, events).
    entry = (key, ts, model, in, out, cache_write_5m, cache_write_1h, cache_read, cwd, sidechain, effort, via,
    mcp_tool, approx, fast, cost_usd, us); fast: usage.speed is "fast"; cost_usd: the call's own costUSD when it is a
    number, else None; us: usage.inference_geo is exactly "us"; approx marks a call ccusage would not have priced the
    way we do (a speed other than standard / fast, a costUSD that is not a number: ccusage drops such a line, we count
    it at the computed price).
    A line that raises or has a wrong type where it matters (a count that is not an int >= 0, a model or id that
    is not a string) is skipped on its own; the lines around it still count.
    events: see event_of."""
    title, entries, events = None, [], []
    try:
        fh = open(path, "r", errors="replace")
    except OSError:
        return title, "main", entries, events
    with fh:
        for line in fh:
            try:
                if '"customTitle"' in line and '"type":"custom-title"' in line:
                    t = json.loads(line).get("customTitle")
                    title = t if isinstance(t, str) and t else title
                    continue
                o = None
                if any(m in line for m in EVENT_MARKS):
                    o = json.loads(line)
                    try:  # an event line in a shape we do not know must not stop the build
                        ev, done = event_of(o) if isinstance(o, dict) else (None, True)
                    except (AttributeError, TypeError, ValueError):
                        ev, done = None, False
                    if ev:
                        events.append(ev)
                    if done:
                        continue
                if '"usage"' not in line or '"assistant"' not in line:
                    continue
                if o is None:
                    o = json.loads(line)
                if not isinstance(o, dict) or o.get("type") != "assistant":
                    continue
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                u = msg.get("usage")
                ts = o.get("timestamp") or o.get("_audit_timestamp")  # Cowork's audit.jsonl has only the second
                model = msg.get("model")
                if not isinstance(u, dict) or not u or not ts or not isinstance(model, str) or not model \
                        or model == "<synthetic>":
                    continue
                ts = norm_ts(ts)
                if not ts:
                    continue
                top = count_of(u.get("cache_creation_input_tokens"))
                cc = u.get("cache_creation")
                if isinstance(cc, dict):  # the 5-minute and 1-hour writes are priced apart
                    c5, c1h = count_of(cc.get("ephemeral_5m_input_tokens")), count_of(cc.get("ephemeral_1h_input_tokens"))
                else:
                    c5, c1h = top, 0
                vals = (count_of(u.get("input_tokens")), count_of(u.get("output_tokens")), c5, c1h,
                        count_of(u.get("cache_read_input_tokens")))
                if not any(vals) and not top:
                    continue
                mid, rid = msg.get("id"), o.get("requestId")
                if not all(v is None or isinstance(v, str) for v in (mid, rid)):
                    continue
                key = (mid, rid) if mid and (rid or "_audit_timestamp" in o) else None  # audit lines have no requestId
                cwd, effort, tool = o.get("cwd"), o.get("effort"), o.get("attributionMcpTool")
                speed, cu = u.get("speed"), o.get("costUSD")
                cost_usd = float(cu) if isinstance(cu, (int, float)) and not isinstance(cu, bool) and math.isfinite(cu) else None
                approx = (("speed" in u and speed not in ("standard", "fast"))
                          or ("costUSD" in o and cost_usd is None))
                entries.append((key, ts, model) + vals + (cwd if isinstance(cwd, str) else None, bool(o.get("isSidechain")),
                                effort if isinstance(effort, str) else "", via_of(o),
                                tool[:40] if isinstance(tool, str) else "", approx, speed == "fast", cost_usd,
                                u.get("inference_geo") == "us"))
            except Exception:  # RecursionError on a line nested too deep, a wrong type, anything else: skip this line only
                continue
    return title, agent_of(path, is_sub_file), entries, events


def jsonl_files(top):
    """Every .jsonl under top; a link inside the tree is not followed and not read, so the walk stays in it."""
    for dirpath, _dirs, names in os.walk(top, followlinks=False):
        for n in names:
            p = os.path.join(dirpath, n)
            if n.endswith(".jsonl") and not os.path.islink(p):
                yield p


def source_of(path, root, extra):
    """(kind, folder) of the source a transcript lies under: root, else one of extra's (kind, folder)."""
    for kind, top in [("claude_code", root)] + list(extra):
        if path.startswith(top.rstrip("/") + "/"):
            return kind, top
    return "claude_code", root


def load_all(cache_path=None, root=ROOT, extra=()):
    """Parse every transcript, reusing cached results for unchanged files. extra: more (kind, folder) sources
    to read besides root, e.g. ("cowork", folder) for the Cowork sessions of the Claude desktop app.
    Returns (files, parsed, skipped): skipped counts the files that still raised in spite of parse_file's own
    per-line protection; such a file is left out (and not cached), the others are read."""
    cache = {}
    if cache_path and os.path.exists(cache_path):
        try:
            with open(cache_path, "rb") as fh:
                blob = pickle.load(fh)
            if blob.get("v") == CACHE_VERSION:
                cache = blob["files"]
        except Exception:
            cache = {}
    out, parsed, skipped, born = {}, 0, 0, {}
    paths = list(jsonl_files(root))
    for _kind, top in extra:
        paths.extend(jsonl_files(top))
    for path in sorted(paths):
        try:
            st = os.stat(path)
        except OSError:
            continue
        sig = (st.st_size, st.st_mtime_ns)
        born[path] = getattr(st, "st_birthtime", st.st_mtime)
        hit = cache.get(path)
        if hit and hit[0] == sig:
            out[path] = hit
        else:
            try:
                is_sub = "subagents" in os.path.relpath(path, source_of(path, root, extra)[1]).split("/")
                out[path] = (sig,) + parse_file(path, is_sub)
                parsed += 1
            except Exception:
                skipped += 1
    if cache_path and (parsed or len(out) != len(cache)):
        tmp = cache_path + ".tmp"
        os.makedirs(os.path.dirname(cache_path), exist_ok=True)
        with open(tmp, "wb") as fh:
            pickle.dump({"v": CACHE_VERSION, "files": out}, fh, protocol=pickle.HIGHEST_PROTOCOL)
        os.replace(tmp, cache_path)
    # Oldest file first: a forked or resumed session copies its parent's messages into a new
    # file, and each message is counted once, for the first file seen. This order credits the
    # original session and charges a fork only for what it added.
    ordered = {p: out[p] for p in sorted(out, key=lambda p: (born[p], p))}
    return ordered, parsed, skipped


DAY = re.compile(r"^20\d\d-\d{2}-\d{2}$")


def real_day(day):
    """Is this key a calendar day (YYYY-MM-DD, year 2000 to 2099)?"""
    try:
        return bool(DAY.match(day)) and datetime.strptime(day, "%Y-%m-%d").strftime("%Y-%m-%d") == day
    except ValueError:
        return False


def clean_day(rows):
    """The rows of one archived day as 12-field rows, or None when the day is not in a shape we can use (not a list
    of rows, a row of another length, a text where a number belongs, a count that is not an int >= 0)."""
    if not isinstance(rows, list):
        return None
    out = []
    for x in rows:
        if not isinstance(x, list) or len(x) not in (11, 12):
            return None
        if len(x) == 11:  # written before the "via" column existed
            x = x[:4] + [""] + x[4:]
        num = lambda v: isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
        cnt = lambda v: isinstance(v, int) and not isinstance(v, bool) and v >= 0
        if not (all(isinstance(v, str) for v in x[:5]) and all(cnt(v) for v in x[5:9]) and num(x[9])
                and num(x[10]) and cnt(x[11])):
            return None
        out.append(x)
    return out


def merge_archive(fact_rows, archive_path, local_day, log):
    """Keep past days in an archive; bring back days whose transcripts are gone.

    Archive: {day: [[model, project, agent, effort, via, in, out, cw, cr, cost, saved, msgs], ...]}
    (rows written before the "via" column existed have 11 fields and are read with via "").
    A past day is restored from the archive when the archive holds more tokens for
    it than the transcripts still do (transcripts only ever disappear).
    A damaged archive never stops the build: a file that is not JSON or not an object is read as empty, a day
    in an unusable shape is ignored, and before the file is rewritten a copy of it is kept next to it
    (archive.json.corrupt-<time>); when that copy cannot be made the archive is left as it is.
    Returns (fact_rows, sorted list of restored days)."""
    if not archive_path:
        return fact_rows, []
    corrupt = False
    try:
        with open(archive_path) as fh:
            raw = json.load(fh)
    except FileNotFoundError:
        raw = {}
    except (OSError, ValueError, RecursionError):
        raw, corrupt = {}, True
    if not isinstance(raw, dict):
        raw, corrupt = {}, True
    archive, changed = {}, False
    for day, old in raw.items():
        rows = clean_day(old) if real_day(day) else None
        if rows is None:
            corrupt = True
        else:
            archive[day] = rows
            changed = changed or rows != old
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    cur = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0.0, 0.0, 0]))
    for r in fact_rows:
        a = cur[local_day(r[0])][(r[1], r[2], r[4], r[5], r[6])]
        for i in range(7):
            a[i] += r[NV + i]

    def tokens(rows):
        return sum(x[5] + x[6] + x[7] + x[8] for x in rows)

    restore = set()
    for day in set(cur) | set(archive):
        if day >= today:
            continue
        now_rows = [list(k) + [v[0], v[1], v[2], v[3], round(v[4], 4), round(v[5], 4), v[6]]
                    for k, v in sorted(cur.get(day, {}).items())]
        old_rows = archive.get(day)
        if old_rows and tokens(old_rows) > tokens(now_rows) * 1.001:
            restore.add(day)
        elif now_rows and now_rows != old_rows:
            archive[day] = now_rows
            changed = True
    if restore:
        fact_rows = [r for r in fact_rows if local_day(r[0]) not in restore]
        for day in sorted(restore):
            noon = datetime.strptime(day + " 12", "%Y-%m-%d %H").astimezone()
            hour = noon.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
            for x in archive[day]:
                fact_rows.append([hour, x[0], x[1], 0 if x[2] == "main" else 1, x[2], x[3], x[4]] + x[5:])
        fact_rows.sort(key=lambda r: r[:NV])
        log(f"restored {len(restore)} day(s) from the archive")
    if changed:
        os.makedirs(os.path.dirname(archive_path), exist_ok=True)
        if corrupt:
            try:
                shutil.copy2(archive_path, archive_path + datetime.now().strftime(".corrupt-%Y%m%d-%H%M%S"))
            except OSError:
                log("archive is damaged and could not be copied; left as it is")
                return fact_rows, sorted(restore)
        tmp = archive_path + ".tmp"
        with open(tmp, "w") as fh:
            json.dump(archive, fh, separators=(",", ":"), ensure_ascii=False)
        os.replace(tmp, archive_path)
    return fact_rows, sorted(restore)


def build(prices, limits, cache_path=None, archive_path=None, log=None, root=ROOT,
          project_roots=(), group_folders=(), extra=()):
    """prices: {model id: entry} as pricing.sanitize gives."""
    log = log or (lambda *a: None)
    files, parsed, skipped = load_all(cache_path, root, extra)
    project_of_ = lambda cwd, folder: project_of(cwd, folder, project_roots, group_folders)
    # (hour_utc_iso, model, project, agent, effort, via) -> [in, out, cw, cr, msgs, cost, saved]
    facts = defaultdict(lambda: [0, 0, 0, 0, 0, 0.0, 0.0])
    # (session, hour) -> {model: [in, out, cw, cr, cost]}
    sess_hour = defaultdict(lambda: defaultdict(lambda: [0, 0, 0, 0, 0.0]))
    sess_info = {}
    calls = []                       # every call, for the costliest-calls list
    ctx_series = defaultdict(list)   # session -> [(ts, context tokens)] of its main thread
    compactions, seen_ev = [], set()
    errors = defaultdict(int)        # (hour, kind, project) -> count
    via_tools = defaultdict(lambda: defaultdict(int))  # connector with an id for a name -> its tools
    day_cache = {}

    def local_day(hour):
        d = day_cache.get(hour)
        if d is None:
            d = day_cache[hour] = parse_ts(hour).astimezone().strftime("%Y-%m-%d")
        return d

    # A message is written several times while it streams, and a forked session copies it again. It
    # counts once: the first line gives the time and (oldest file first) the attribution, the line with
    # the most tokens (input + output + both cache writes + cache read; a tie: the higher cost, else the
    # first) gives the tokens, speed and costUSD. That is ccusage's rule (research/pricing/FINDINGS.md, case 6);
    # ccusage also takes the day from that line, we keep the first line's time.
    # rec = [ts, model, in, out, c5m, c1h, cr, approx, folder, session, is_sub_file, file_agent,
    #        cwd, sidechain, effort, via, tool, fast, cost_usd, us]
    price_of = {}

    def line_cost(r):
        if r[18] is not None:
            return r[18]
        p = price_of.get(r[1], False)
        if p is False:
            p = price_of[r[1]] = pricing.find(prices, r[1])
        return pricing.message_cost(p, r[2], r[3], r[4], r[5], r[6], r[17], r[19]) if p else 0.0

    msgs = {}
    loose = []  # lines without a message id and request id are counted as they are
    for path, (_sig, title, file_agent, entries, events) in files.items():
        kind, top = source_of(path, root, extra)
        rel = os.path.relpath(path, top).split("/")
        is_sub_file = "subagents" in rel
        cowork = kind == "cowork"
        if cowork:  # one project for all of Cowork (the cwd of its lines is a sandbox); session = the local_<id> folder
            folder = "Cowork"
            session = next((x[6:] for x in rel[:-1] if x.startswith("local_") and len(x) > 6), rel[-1][:-6])
        else:
            folder = rel[0]
            session = rel[1] if len(rel) > 2 else rel[1][:-6]
        if title:
            sess_info.setdefault(session, {})["title"] = title
        for ev in events:
            if ev[1] is not None:
                if ev[1] in seen_ev:
                    continue
                seen_ev.add(ev[1])
            if ev[0] == "c":
                compactions.append((ev[2], session, project_of_(None if cowork else ev[6], folder), ev[3], ev[4], ev[5], not is_sub_file))
            else:
                errors[(ev[2][:13] + ":00:00Z", ev[3], project_of_(None if cowork else ev[4], folder))] += 1
        for key, ts, model, vin, vout, c5, c1h, vcr, cwd, sidechain, effort, via, tool, approx, fast, cost_usd, us in entries:
            rec = [ts, model, vin, vout, c5, c1h, vcr, approx, folder, session, is_sub_file, file_agent,
                   None if cowork else cwd, sidechain, effort, via, tool, fast, cost_usd, us]
            if cowork and key:  # the two Cowork copies of a call differ in requestId (audit lines have none)
                key = (key[0], None)
            if key is None:
                loose.append(rec)
                continue
            first = msgs.get(key)
            if first is None:
                msgs[key] = rec
                continue
            ts0 = min(first[0], ts)
            n_new, n_old = sum(rec[2:7]), sum(first[2:7])
            if n_new > n_old or (n_new == n_old and line_cost(rec) > line_cost(first)):
                first[1:8] = rec[1:8]
                first[17:20] = rec[17:20]
            first[0] = ts0

    unpriced, n_approx, n_us = set(), 0, 0
    for rec in list(msgs.values()) + loose:
        ts, model, vin, vout, c5, c1h, vcr, approx, folder, session, is_sub_file, file_agent, cwd, sidechain, \
            effort, via, tool, fast, cost_usd, us = rec
        if model not in price_of:
            price_of[model] = pricing.find(prices, model)
        p = price_of[model]
        if p is None:
            if cost_usd is None:
                unpriced.add(model.replace("claude-", ""))
            cost, saved = (cost_usd or 0.0), 0.0
        else:
            cost = pricing.message_cost(p, vin, vout, c5, c1h, vcr, fast, us) if cost_usd is None else cost_usd
            saved = pricing.cache_saved(p, vin, c5, c1h, vcr, fast, us)
        n_approx += bool(approx)
        n_us += us
        model = model.replace("claude-", "")
        vcw = c5 + c1h
        hour = ts[:13] + ":00:00Z"
        proj = project_of_(cwd, folder)
        agent = file_agent if is_sub_file else ("unknown" if sidechain else "main")
        f = facts[(hour, model, proj, agent, effort, via)]
        if tool and UUID.match(via[4:]):
            via_tools[via][tool] += 1
        calls.append((cost, ts, hour, session, proj, model, agent, effort, vin + vcw + vcr, vout))
        if agent == "main":
            ctx_series[session].append((ts, vin + vcw + vcr))
        sh = sess_hour[(session, hour)][model]
        for i, v in enumerate((vin, vout, vcw, vcr)):
            f[i] += v
            sh[i] += v
        f[4] += 1
        f[5] += cost
        f[6] += saved
        sh[4] += cost
        si = sess_info.setdefault(session, {})
        if agent == "main" or "project" not in si:
            si["project"] = proj
        if "start" not in si or ts < si["start"]:
            si["start"] = ts
        if "end" not in si or ts > si["end"]:
            si["end"] = ts
        si["msgs"] = si.get("msgs", 0) + 1

    check = {"files": len(files), "files_parsed": parsed, "unpriced_models": sorted(unpriced),
             "approx_pricing": n_approx, "skipped_files": skipped, "us_geo_calls": n_us}
    log(f"files={len(files)} parsed={parsed} skipped={skipped} messages={len(msgs) + len(loose):,}")
    if unpriced:
        log("UNPRICED models:", sorted(unpriced))
    if n_approx:
        log(f"{n_approx} call(s) with a speed or costUSD the pricing rules do not know, counted at the computed price")

    # --- output ---------------------------------------------------------------
    now = datetime.now(timezone.utc)
    fact_rows = []
    for (hour, model, proj, agent, effort, via), v in sorted(facts.items()):
        fact_rows.append([hour, model, proj, 0 if agent == "main" else 1, agent, effort, via,
                          v[0], v[1], v[2], v[3], round(v[5], 4), round(v[6], 4), v[4]])
    live_cost = sum(r[NV + 4] for r in fact_rows)
    fact_rows, restored = merge_archive(fact_rows, archive_path, local_day, log)
    check["restored_days"] = len(restored)
    check["restored"] = restored  # these days have no hour of day any more (rows sit at noon)

    sess_ids, sessions, sess_hours = {}, [], []
    sess_days = defaultdict(lambda: [0.0, 0])
    for (session, hour), models in sorted(sess_hour.items()):
        cost = sum(v[4] for v in models.values())
        tok = sum(sum(v[:4]) for v in models.values())
        if session not in sess_ids:
            si = sess_info.get(session, {})
            sess_ids[session] = len(sessions)
            sessions.append([session[:8], si.get("title") or "", si.get("project") or "",
                             si.get("start") or hour, si.get("end") or hour, si.get("msgs", 0)])
        idx = sess_ids[session]
        d = sess_days[(idx, local_day(hour))]
        d[0] += cost
        d[1] += tok
        if (now - parse_ts(hour)).total_seconds() <= SESSION_HOURS_WINDOW * 3600:
            sess_hours.append([idx, hour, round(cost, 4), tok])
    sess_day_rows = [[i, day, round(v[0], 4), v[1]] for (i, day), v in sorted(sess_days.items())]
    log(f"cost priced={live_cost:.2f} facts={len(fact_rows)} sessions={len(sessions)}")

    # Costliest single calls: the top few of every local day, so any period can be answered.
    per_day = defaultdict(list)
    for cost, ts, hour, session, proj, model, agent, effort, ctx, vout in calls:
        per_day[local_day(hour)].append((cost, ts, sess_ids.get(session, -1), proj, model, agent, effort, ctx, vout))
    call_rows = []
    for day in sorted(per_day):
        for c in heapq.nlargest(TOP_CALLS_PER_DAY, per_day[day]):
            call_rows.append([c[1], c[2], c[3], c[4], c[5], c[6], c[7], c[8], round(c[0], 4)])

    # Context size per call along each session's main thread (thinned to CTX_POINTS, keeping peaks),
    # with the positions where the context was compacted.
    marks = defaultdict(list)
    for ts, session, _proj, _trig, _pre, _post, is_main in compactions:
        if is_main:
            marks[session].append(ts)
    sess_ctx = {}
    for session, pts in ctx_series.items():
        if len(pts) < 5 or session not in sess_ids:
            continue
        pts.sort()
        n = len(pts)
        b = min(CTX_POINTS, n)
        vals = [0] * b
        for i, (_ts, ctx) in enumerate(pts):
            j = i * b // n
            vals[j] = max(vals[j], ctx)
        times = [p[0] for p in pts]
        where = sorted({min(b - 1, bisect.bisect_left(times, t) * b // n) for t in marks.get(session, [])})
        sess_ctx[str(sess_ids[session])] = [[round(v / 1000) for v in vals], where, n]
    compaction_rows = [[ts, sess_ids.get(session, -1), proj, trig, pre, post]
                       for ts, session, proj, trig, pre, post, _m in sorted(compactions)]
    error_rows = [[h, k, p, n] for (h, k, p), n in sorted(errors.items())]

    return {
        "meta": [{
            "generated_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "first_hour": fact_rows[0][0] if fact_rows else None,
            "last_hour": fact_rows[-1][0] if fact_rows else None,
            "plan": limits.get("plan"),
            "sessions": len(sessions),
            "check": check,
        }],
        "limits": limits.get("windows", []),
        "fact_cols": FACT_COLS,
        "facts": fact_rows,
        "session_cols": ["id", "title", "project", "start", "end", "msgs"],
        "sessions": sessions,
        "sess_day_cols": ["s", "day", "cost", "tokens"],
        "sess_days": sess_day_rows,
        "sess_hour_cols": ["s", "hour", "cost", "tokens"],
        "sess_hours": sess_hours,
        "call_cols": ["t", "s", "project", "model", "agent", "effort", "context", "output", "cost"],
        "calls": call_rows,
        "sess_ctx": sess_ctx,
        # connectors named by an id: their most used tool, so the page can show which one it is
        "via_hint": {v: max(c.items(), key=lambda kv: (kv[1], kv[0]))[0] for v, c in via_tools.items()},
        "compaction_cols": ["t", "s", "project", "trigger", "pre", "post"],
        "compactions": compaction_rows,
        "error_cols": ["hour", "kind", "project", "n"],
        "errors": error_rows,
    }


def main():
    prices_path, limits_path, out_path = sys.argv[1:4]
    cache_path = sys.argv[4] if len(sys.argv) > 4 else None
    archive_path = sys.argv[5] if len(sys.argv) > 5 else None
    out = build(pricing.sanitize(json.load(open(prices_path))["models"]), json.load(open(limits_path)), cache_path, archive_path,
                log=lambda *a: print(*a, file=sys.stderr))
    with open(out_path, "w") as fh:
        json.dump(out, fh, separators=(",", ":"), ensure_ascii=False)
    print(f"bytes={os.path.getsize(out_path):,}", file=sys.stderr)


if __name__ == "__main__":
    main()
