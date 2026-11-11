#!/usr/bin/env python3
"""Check what parse_file reads from a transcript: usage entries, the skill or MCP
server of a call, compactions and API errors; that a line of a shape we do not know is skipped
without stopping the lines around it or the build; and that only real files under projects/ are read.
Uses made-up transcripts in temporary folders.  Run: python3 tests/test_parse.py
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import build as B

TS = "2026-11-10T10:00:00.000Z"


def call(i, text="hi", **extra):
    o = {"type": "assistant", "timestamp": TS, "uuid": f"u{i}", "requestId": f"r{i}", "cwd": "/x",
         "message": {"id": f"m{i}", "model": "claude-sonnet-5-5", "content": [{"type": "text", "text": text}],
                     "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100,
                               "cache_creation_input_tokens": 20}}}
    o.update(extra)
    return o


LINES = [
    call(1),
    call(2, attributionMcpServer="reddit", attributionSkill="research", attributionMcpTool="post"),
    call(3, attributionSkill="research"),
    # a normal message that only talks about these words must still be counted
    call(4, text='the "compact_boundary" and "api_error" lines, "isApiErrorMessage":true'),
    # the cache write is read from the 5-minute / 1-hour split when it exists, and a fast call is marked
    {**call(5), "message": {"id": "m5", "model": "claude-sonnet-5-5", "content": [], "usage": {
        "input_tokens": 1, "output_tokens": 2, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 99,
        "speed": "fast", "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 6}}}},
    {"type": "system", "subtype": "compact_boundary", "uuid": "c1", "timestamp": TS, "cwd": "/x",
     "compactMetadata": {"trigger": "auto", "preTokens": 400000, "postTokens": 30000}},
    {"type": "system", "subtype": "api_error", "uuid": "e1", "timestamp": TS, "cwd": "/x",
     "error": {"formatted": "529 overloaded", "isNetworkDown": False, "noResponse": False}},
    {"type": "system", "subtype": "api_error", "uuid": "e2", "timestamp": TS, "error": {"isNetworkDown": True}},
    {"type": "system", "subtype": "api_error", "uuid": "e3", "timestamp": TS, "error": "odd"},
    {"type": "assistant", "isApiErrorMessage": True, "error": "rate_limit", "uuid": "e4", "timestamp": TS,
     "message": {"model": "<synthetic>", "usage": {"input_tokens": 0}}},
    {"type": "system", "subtype": "compact_boundary", "uuid": "c2", "timestamp": "not a time"},
    # shapes we do not know: skipped or read with defaults, never an exception
    {"type": "system", "subtype": "compact_boundary", "uuid": [1], "timestamp": TS, "compactMetadata": "str"},
    {"type": "system", "subtype": "api_error", "uuid": {"a": 1}, "timestamp": TS, "error": {"formatted": ["x"]}},
]


def test_main_lines():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.jsonl")
        with open(path, "w") as fh:
            for o in LINES:
                fh.write(json.dumps(o, separators=(",", ":")) + "\n")
            fh.write('{"type":"system","subtype":"api_error", broken json\n')
            fh.write('[1, "compact_boundary"]\n')
        title, agent, entries, events = B.parse_file(path, False)
    assert agent == "main" and title is None
    assert len(entries) == 5, len(entries)
    assert [e[11] for e in entries] == ["", "mcp:reddit", "skill:research", "", ""], [e[11] for e in entries]
    assert all(e[3:8] == (10, 5, 20, 0, 100) for e in entries[:4])   # no split: all of it is a 5-minute write
    assert entries[4][3:8] == (1, 2, 4, 6, 3) and [e[13] for e in entries] == [False] * 5   # fast is a priced rule now
    assert [e[14] for e in entries] == [False] * 4 + [True] and [e[15] for e in entries] == [None] * 5   # fast, costUSD
    assert entries[0][2] == "claude-sonnet-5-5"                      # the full model id, for pricing
    kinds = sorted(e[3] for e in events if e[0] == "e")
    assert kinds == ["final:rate_limit", "retry:529", "retry:network", "retry:other", "retry:other"], kinds
    comps = [e for e in events if e[0] == "c"]
    assert len(comps) == 2 and comps[0][3:6] == ("auto", 400000, 30000) and comps[1][1] is None, comps
    assert [e[12] for e in entries] == ["", "post", "", "", ""], [e[12] for e in entries]
    assert [e[16] for e in entries] == [False] * 5                   # inference_geo "us" is not set
    return f"{len(entries)} calls, {len(kinds)} errors, {len(comps)} compaction read"


def dump(o):
    return json.dumps(o, separators=(",", ":"))


def read(*lines):
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "s.jsonl")
        with open(path, "w") as fh:
            fh.write("\n".join(lines) + "\n")
        return B.parse_file(path, False)


def with_usage(**u):
    o = call(9)
    o["message"]["usage"].update(u)
    return o


def test_bad_lines():
    """Each bad line sits between two good ones: the good ones are counted, the bad one never raises."""
    deep = '{"type":"assistant","message":{"usage":' + "[" * 100000 + "]" * 100000 + "}}"
    bad = {
        "nested 100000 deep": deep,
        "message is a string": dump({**call(9), "message": "assistant usage"}),
        "message is a list": dump({**call(9), "message": [{"usage": 1}]}),
        "usage is a list": dump({**call(9), "message": {**call(9)["message"], "usage": [1, 2]}}),
        "input_tokens is a string": dump(with_usage(input_tokens="10")),
        "output_tokens is a float": dump(with_usage(output_tokens=5.5)),
        "cache_read is a list": dump(with_usage(cache_read_input_tokens=[1])),
        "cache_creation_input_tokens negative": dump(with_usage(cache_creation_input_tokens=-5)),
        "token count is a bool": dump(with_usage(input_tokens=True)),
        "5m write is a string": dump(with_usage(cache_creation={"ephemeral_5m_input_tokens": "4"})),
        "model is an int": dump({**call(9), "message": {**call(9)["message"], "model": 5}}),
        "model is a list": dump({**call(9), "message": {**call(9)["message"], "model": ["a"]}}),
        "message.id is a list": dump({**call(9), "message": {**call(9)["message"], "id": [1]}}),
        "requestId is a dict": dump({**call(9), "requestId": {"a": 1}}),
        "timestamp is an int": dump({**call(9), "timestamp": 5}),
        "costUSD is huge": dump({**call(9), "costUSD": 10 ** 400}),             # float() of it overflows
        "line is a list": '["assistant","usage"]',
        "event line nested deep": '{"type":"system","subtype":"api_error","x":' + "[" * 100000 + "]" * 100000 + "}",
        "title line is a list": '[{"type":"custom-title","customTitle":"x"}] "customTitle" "type":"custom-title"',
    }
    for name, line in bad.items():
        title, agent, entries, events = read(dump(call(1)), line, dump(call(2)))
        assert [e[0] for e in entries] == [("m1", "r1"), ("m2", "r2")], (name, [e[0] for e in entries])
        assert title is None and agent == "main", name
    # a cwd that is not a string keeps the call (its project falls back to the folder) and drops only the cwd
    _t, _a, entries, _e = read(dump(call(1)), dump({**call(2), "cwd": 5}), dump({**call(3), "cwd": ["x"]}), dump(call(4)))
    assert [e[0] for e in entries] == [("m1", "r1"), ("m2", "r2"), ("m3", "r3"), ("m4", "r4")]
    assert [e[8] for e in entries] == ["/x", None, None, "/x"]
    # the same in events: a cwd that is not a string is dropped, the event stays
    ev = dump({"type": "system", "subtype": "api_error", "uuid": "e1", "timestamp": TS, "cwd": 5, "error": {"formatted": "529"}})
    _t, _a, entries, events = read(ev, dump(call(1)))
    assert len(entries) == 1 and events[0][4] is None and events[0][3] == "retry:529", events
    # a title that is not text is ignored; a good one is kept
    t_bad = dump({"type": "custom-title", "customTitle": ["a"]})
    t_ok = dump({"type": "custom-title", "customTitle": "Name"})
    assert read(t_bad, dump(call(1)))[0] is None and read(t_bad, t_ok)[0] == "Name"
    # inference_geo is read exactly: "us" only
    _t, _a, entries, _e = read(*(dump(with_usage(inference_geo=g)).replace('"m9"', '"m%d"' % i).replace('"r9"', '"r%d"' % i)
                                  for i, g in enumerate(("us", "US", "global", "eu", "", None, ["us"]))))
    assert [e[16] for e in entries] == [True, False, False, False, False, False, False], [e[16] for e in entries]

    # a meta file that is not an object does not turn a subagent file into a failure
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "agent-a.jsonl")
        with open(path, "w") as fh:
            fh.write(dump(call(1)) + "\n")
        for body in ("[1]", "{broken", "[" * 100000 + "]" * 100000, '{"agentType": 5}'):
            with open(path[:-6] + ".meta.json", "w") as fh:
                fh.write(body)
            _t, agent, entries, _e = B.parse_file(path, True)
            assert len(entries) == 1 and agent in ("unknown", "5"), (body[:10], agent)


def test_bad_file_and_build():
    """A file that raises in spite of the line protection is skipped and counted; the others are read."""
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        for name, n in (("-x/aaaaaaaa-1.jsonl", 1), ("-x/bbbbbbbb-2.jsonl", 2), ("-y/cccccccc-3.jsonl", 3)):
            os.makedirs(os.path.dirname(os.path.join(root, name)), exist_ok=True)
            with open(os.path.join(root, name), "w") as fh:
                fh.write(dump(call(n)) + "\n")
        real = B.parse_file

        def flaky(path, is_sub):
            if "bbbbbbbb" in path:
                raise RuntimeError("boom")
            return real(path, is_sub)

        B.parse_file = flaky
        try:
            cache = os.path.join(tmp, "parsed.pickle")
            files, parsed, skipped = B.load_all(cache, root)
            assert (len(files), parsed, skipped) == (2, 2, 1), (len(files), parsed, skipped)
            prices = {}
            out = B.build(prices, {"windows": []}, cache, None, root=root)
            chk = out["meta"][0]["check"]
            assert chk["files"] == 2 and chk["skipped_files"] == 1 and chk["files_parsed"] == 0, chk   # the good ones came from the cache
            assert sum(r[-1] for r in out["facts"]) == 2
        finally:
            B.parse_file = real
        out = B.build({}, {"windows": []}, None, None, root=root)
        assert out["meta"][0]["check"]["skipped_files"] == 0 and sum(r[-1] for r in out["facts"]) == 3


def test_links_not_read():
    """Under projects/ a link to a folder and a link to a file are not read (the main root is walked like the others)."""
    with tempfile.TemporaryDirectory() as tmp:
        root, outside = os.path.join(tmp, "projects"), os.path.join(tmp, "outside")
        for d, n in ((os.path.join(root, "-x"), 1), (outside, 2)):
            os.makedirs(d)
            with open(os.path.join(d, "s%d.jsonl" % n), "w") as fh:
                fh.write(dump(call(n)) + "\n")
        os.symlink(outside, os.path.join(root, "-linked-dir"))
        os.symlink(os.path.join(outside, "s2.jsonl"), os.path.join(root, "-x", "linked-file.jsonl"))
        files, _parsed, _skipped = B.load_all(None, root)
        assert list(files) == [os.path.join(root, "-x", "s1.jsonl")], list(files)   # the key is root + relative path, as before
        assert [e[0] for e in files[os.path.join(root, "-x", "s1.jsonl")][3]] == [("m1", "r1")]


def test_stray_file():
    """A .jsonl lying directly in projects/ does not stop the build; the sessions in project folders still count."""
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        os.makedirs(os.path.join(root, "-x"))
        for path, n in ((os.path.join(root, "stray.jsonl"), 1), (os.path.join(root, "-x", "s2.jsonl"), 2)):
            with open(path, "w") as fh:
                fh.write(dump(call(n)) + "\n")
        out = B.build({}, {"windows": []}, None, None, root=root)
        assert sum(r[-1] for r in out["facts"]) == 1, out["facts"]


def main():
    test_stray_file()
    summary = test_main_lines()
    test_bad_lines()
    test_bad_file_and_build()
    test_links_not_read()
    print(f"ok: {summary}; bad lines (deep nesting, wrong types) skipped without stopping the lines around them; "
          "a file that raises is skipped and counted; links under projects/ not read")


if __name__ == "__main__":
    main()
