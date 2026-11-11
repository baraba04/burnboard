#!/usr/bin/env python3
"""Check app/pricing.py and the pricing in build.py with made-up price entries and transcripts.

Nothing real is read: temporary folders only, no archive, and no request to the internet (the price list
fetch is injected; a small server on 127.0.0.1 stands in for it where fetch() itself is tested).
Run: python3 tests/test_pricing.py
"""
import http.server
import json
import math
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

APP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app")
sys.path.insert(0, APP)
os.environ["USAGE_CLAUDE_OFFLINE"] = "1"  # load() must not fetch unless a test lifts this
import build as B  # noqa: E402
import pricing as P  # noqa: E402

BASE = {"input_cost_per_token": 1e-6, "output_cost_per_token": 5e-6, "cache_creation_input_token_cost": 1.25e-6,
        "cache_creation_input_token_cost_above_1hr": 2e-6, "cache_read_input_token_cost": 1e-7}


def scaled(f, n):
    return {k + "_above_%dk_tokens" % n: v * f for k, v in BASE.items()}


RAW = {
    "claude-a": dict(BASE, litellm_provider="anthropic", mode="chat"),
    "claude-nohr": {k: v for k, v in BASE.items() if k != "cache_creation_input_token_cost_above_1hr"},
    "claude-t": dict(BASE, **scaled(2, 100), **scaled(3, 200)),           # both tiers
    "claude-t100": dict(BASE, **scaled(2, 100)),
    "claude-gap": dict(BASE, input_cost_per_token_above_100k_tokens=4e-6),  # a tier key missing on four fields
    "claude-1m": dict(BASE, **scaled(10, 1000)),                          # any N, not only 100 and 200
    "anthropic/claude-p": dict(BASE, input_cost_per_token=9e-6),
    "anthropic/claude-b": dict(BASE, input_cost_per_token=9e-6),
    "claude-b": dict(BASE, input_cost_per_token=7e-6),                    # the bare key wins over its twin
    "claude-batch": dict(BASE, input_cost_per_token_above_100k_tokens_batches=1.0),  # not a tier key
    "gpt-x": dict(BASE),                                                  # not a Claude entry
}
PRICES = P.sanitize(RAW)
close = lambda a, b: abs(a - b) < 1e-12


def cost(model, i=0, out=0, e5=0, e1=0, cr=0):
    return P.message_cost(PRICES[model], i, out, e5, e1, cr)


def test_base_and_cache_split():
    assert close(cost("claude-a", i=1000), 1e-3)
    assert close(cost("claude-a", out=1000), 5e-3)
    assert close(cost("claude-a", e5=1000), 1.25e-3)            # 5-minute write
    assert close(cost("claude-a", e1=1000), 2e-3)               # 1-hour write, its own price
    assert close(cost("claude-a", cr=1000), 1e-4)
    assert close(cost("claude-a", 1000, 1000, 1000, 1000, 1000), 1e-3 + 5e-3 + 1.25e-3 + 2e-3 + 1e-4)
    assert close(cost("claude-nohr", e1=1000), 1.25e-3)         # no 1-hour price: the 5-minute one
    assert close(P.cache_saved(PRICES["claude-a"], 0, 0, 0, 1000), 1000 * (1e-6 - 1e-7))
    assert "gpt-x" not in PRICES and "anthropic/claude-p" not in PRICES and "claude-p" in PRICES
    assert close(PRICES["claude-b"]["input_cost_per_token"], 7e-6)
    assert PRICES["claude-batch"]["_tiers"] == []               # `_batches` keys are not tiers


def test_tiers():
    # made-up prices; the proven rules with real numbers are in test_ccusage_rules below
    assert close(cost("claude-t100", i=100000), 0.1)               # exactly on the threshold: base price ('>')
    assert close(cost("claude-t100", i=100001), 100001 * 2e-6)
    # the whole message switches, counting input + both cache writes + cache read
    assert close(cost("claude-t100", i=50000, e5=30000, e1=10000, cr=10001),
                 50000 * 2e-6 + 30000 * 2.5e-6 + 10000 * 4e-6 + 10001 * 2e-7)
    assert close(cost("claude-t100", out=1000, i=100001), 100001 * 2e-6 + 1000 * 10e-6)   # output does not count
    assert PRICES["claude-t"]["_tiers"] == [100, 200]
    # any N other than 200 behaves like 100
    assert close(cost("claude-1m", i=1000000), 1.0) and close(cost("claude-1m", i=1000001), 1000001 * 1e-5)
    # a tier key missing on a field: that field keeps its base price
    assert close(cost("claude-gap", i=100001, out=1000), 100001 * 4e-6 + 1000 * 5e-6)
    assert close(cost("claude-gap", cr=100001), 100001 * 1e-7)
    assert close(P.cache_saved(PRICES["claude-t100"], 0, 0, 0, 200000), 200000 * (2e-6 - 2e-7))


# Prices as ccusage 20.0.48 priced them on 11 Nov 2026 (LiteLLM list), and its costs for the made-up calls of
# research/pricing/probe.py (the case name is given with each number; FINDINGS.md has the tables).
CC = P.sanitize({
    "claude-sonnet-4-5-20250929-v1:0": {
        "input_cost_per_token": 3e-6, "output_cost_per_token": 1.5e-5, "cache_creation_input_token_cost": 3.75e-6,
        "cache_creation_input_token_cost_above_1hr": 6e-6, "cache_read_input_token_cost": 3e-7,
        "input_cost_per_token_above_200k_tokens": 6e-6, "output_cost_per_token_above_200k_tokens": 2.25e-5,
        "cache_creation_input_token_cost_above_200k_tokens": 7.5e-6,
        "cache_creation_input_token_cost_above_1hr_above_200k_tokens": 1.2e-5,
        "cache_read_input_token_cost_above_200k_tokens": 6e-7},
    "claude-haiku-5-5": {
        "input_cost_per_token": 1e-7, "output_cost_per_token": 5e-7, "cache_creation_input_token_cost": 1.25e-7,
        "cache_creation_input_token_cost_above_1hr": 2e-7, "cache_read_input_token_cost": 1e-8,
        "input_cost_per_token_above_100k_tokens": 5e-7, "output_cost_per_token_above_100k_tokens": 2.5e-6,
        "cache_creation_input_token_cost_above_100k_tokens": 6.25e-7,
        "cache_creation_input_token_cost_above_1hr_above_100k_tokens": 1e-6,
        "cache_read_input_token_cost_above_100k_tokens": 5e-8, "provider_specific_entry": {"us": 1.1}},
    "claude-sonnet-5-5": {
        "input_cost_per_token": 2e-6, "output_cost_per_token": 1e-5, "cache_creation_input_token_cost": 2.5e-6,
        "cache_creation_input_token_cost_above_1hr": 4e-6, "cache_read_input_token_cost": 1e-7,
        "provider_specific_entry": {"us": 1.1}},
    "claude-opus-5-5": {
        "input_cost_per_token": 4e-6, "output_cost_per_token": 2e-5, "cache_creation_input_token_cost": 5e-6,
        "cache_creation_input_token_cost_above_1hr": 8e-6, "cache_read_input_token_cost": 2e-7,
        "provider_specific_entry": {"us": 1.1, "fast": 2}},
    "claude-opus-4-6": {   # no fast key in the list: ccusage has its own multiplier
        "input_cost_per_token": 5e-6, "output_cost_per_token": 2.5e-5, "cache_creation_input_token_cost": 6.25e-6,
        "cache_creation_input_token_cost_above_1hr": 1e-5, "cache_read_input_token_cost": 5e-7,
        "provider_specific_entry": {"us": 1.1}},
    "claude-opus-4-7-20260416": {
        "input_cost_per_token": 5e-6, "output_cost_per_token": 2.5e-5, "cache_creation_input_token_cost": 6.25e-6,
        "cache_creation_input_token_cost_above_1hr": 1e-5, "cache_read_input_token_cost": 5e-7},
    "claude-opus-4-8": {
        "input_cost_per_token": 5e-6, "output_cost_per_token": 2.5e-5, "cache_creation_input_token_cost": 6.25e-6,
        "cache_creation_input_token_cost_above_1hr": 1e-5, "cache_read_input_token_cost": 5e-7,
        "provider_specific_entry": {"us": 1.1, "fast": 2.0}},
    "claude-fable-5-2": {
        "input_cost_per_token": 8e-6, "output_cost_per_token": 4e-5, "cache_creation_input_token_cost": 1e-5,
        "cache_creation_input_token_cost_above_1hr": 1.6e-5, "cache_read_input_token_cost": 2e-7,
        "provider_specific_entry": {"us": 1.1, "fast": 2}},
})
S45, H55 = "claude-sonnet-4-5-20250929-v1:0", "claude-haiku-5-5"
STD = dict(i=1000, out=1000, e5=1000, e1=1000, cr=1000)   # probe.py `std`: ccusage reports 2000 cache-write tokens


def cc(model, expect, fast=False, **tok):
    got = P.message_cost(CC[model], tok.get("i", 0), tok.get("out", 0), tok.get("e5", 0), tok.get("e1", 0),
                         tok.get("cr", 0), fast)
    assert abs(got - expect) < 1e-9, (model, tok, fast, got, expect)


def test_ccusage_rules():
    # case 3 and 4: a 200k tier splits each token type at 200,000 tokens of its own; '>' (on the threshold = base)
    cc(S45, 0.599997, i=199999)                 # tier_s45_in_-1
    cc(S45, 0.6, i=200000)                      # tier_s45_in_+0
    cc(S45, 0.600006, i=200001)                 # tier_s45_in_+1: 200000 * 3e-6 + 1 * 6e-6, not 200001 * 6e-6
    cc(S45, 0.0600006, cr=200001)               # tier_s45_cr_+1
    cc(S45, 0.7500075, e5=200001)               # tier_s45_c5_+1
    cc(S45, 1.200012, e1=200001)                # tier_s45_c1_+1 (the 1-hour write has its own above-200k price)
    cc(S45, 3.0000525, i=10, out=200001)        # tier_s45_out_+1: output counts on its own
    cc(S45, 4.40997795, i=199999, out=199999, e5=199999, cr=199999)   # tier_s45_each_below: total 800k, no tier
    cc(S45, 0.975006, e5=100000, e1=100001)     # tier_s45_cw_split: the two writes are not added up
    cc(S45, 1.9500195, e5=200001, e1=200001)    # tier_s45_cw_c5_big
    cc(S45, 0.3547503, i=100000, out=1000, e5=1000, e1=1000, cr=100001)    # tier_s45_whole
    cc(S45, 0.80475, i=200000, out=1000, e5=1000, e1=1000, cr=400000)      # tier_s45_whole_big
    # case 3 and 4: any other tier (haiku-5-5 at 100k) switches the whole message when input + both writes +
    # cache read is above the threshold; output does not count
    cc(H55, 0.0099999, i=99999)                 # tier_h55_in_-1
    cc(H55, 0.01, i=100000)                     # tier_h55_in_+0: exactly on the threshold is the base price
    cc(H55, 0.0500005, i=100001)                # tier_h55_in_+1: all 100001 tokens at 5e-7
    cc(H55, 0.00500005, cr=100001)              # tier_h55_cr_+1
    cc(H55, 0.062500625, e5=100001)             # tier_h55_c5_+1
    cc(H55, 0.100001, e1=100001)                # tier_h55_c1_+1
    cc(H55, 0.0500015, i=10, out=100001)        # tier_h55_out_+1: output over the threshold changes nothing
    cc(H55, 0.05437505, i=25000, e5=25000, e1=25000, cr=25001)   # tier_h55_split_+1: no field above, total above
    cc(H55, 0.010875, i=25000, e5=25000, e1=25000, cr=25000)     # tier_h55_split_0
    cc(H55, 0.03162505, i=50000, out=1000, e5=1000, e1=1000, cr=50001)   # tier_h55_whole
    cc(H55, 0.064125, i=100000, out=1000, e5=1000, e1=1000, cr=200000)   # tier_h55_whole_big
    # no tier key in the list entry, no tier (opus-5-5 at 250k, claude-sonnet-5-5 at 900k)
    cc("claude-opus-5-5", 1.0, i=250000)        # tier_o55_250k
    cc("claude-sonnet-5-5", 1.8, i=900000)      # tier_s55_1m
    # the cache saving follows the same split
    assert close(P.cache_saved(CC[S45], 0, 0, 0, 200001), 200000 * (3e-6 - 3e-7) + 1 * (6e-6 - 6e-7))
    assert close(P.cache_saved(CC[H55], 25000, 25000, 25000, 100001), 100001 * (5e-7 - 5e-8))


def test_fast_mode():
    # case 1: speed "fast" multiplies the whole cost: the list's provider_specific_entry.fast, else ccusage's table
    cc("claude-opus-5-5", 0.0372, **STD)                            # speed_O_standard
    cc("claude-opus-5-5", 0.0744, fast=True, **STD)                 # speed_O_fast (2, from the list)
    cc("claude-fable-5-2", 0.0742, **STD)                           # speed_F_standard
    cc("claude-fable-5-2", 0.1484, fast=True, **STD)                # speed_F_fast
    cc("claude-opus-4-8", 0.09350, fast=True, **STD)                # speed_opus48_fast
    cc("claude-opus-4-6", 0.04675, **STD)                           # speed_opus46_standard
    cc("claude-opus-4-6", 0.2805, fast=True, **STD)                 # speed_opus46_fast: 6, not in the list
    cc("claude-opus-4-7-20260416", 0.2805, fast=True, **STD)        # ratio_claude-opus-4-7-20260416: 6 by id prefix
    cc("claude-sonnet-5-5", 0.0186, fast=True, **STD)               # speed_S_fast: no fast price, factor 1
    cc("claude-opus-5-5", 2.04, fast=True, i=250000, out=1000)      # speed_O_fast_tier_big: no long-context change
    assert CC["claude-opus-4-6"]["_fast"] == 6.0 and CC["claude-opus-4-7-20260416"]["_fast"] == 6.0
    assert CC["claude-opus-5-5"]["_fast"] == 2.0 and "_fast" not in CC["claude-sonnet-5-5"]
    # an unusable list value falls back to ccusage's table, or to none
    bad = P.sanitize({"claude-opus-4-6": dict(BASE, provider_specific_entry={"fast": float("nan")}),
                      "claude-x": dict(BASE, provider_specific_entry={"fast": True}),
                      "claude-y": dict(BASE, provider_specific_entry={"fast": -2}),
                      "claude-z": dict(BASE, provider_specific_entry={"fast": 1e9}),
                      "claude-w": dict(BASE, provider_specific_entry="fast")})
    assert bad["claude-opus-4-6"]["_fast"] == 6.0 and not any("_fast" in bad[k] for k in ("claude-x", "claude-y", "claude-z", "claude-w"))
    # the cache saving scales the same way
    assert close(P.cache_saved(CC["claude-opus-5-5"], 0, 0, 0, 1000, True), 2 * 1000 * (4e-6 - 2e-7))
    # the factor survives the stored copy and the shipped list (offline pricing of a fast call)
    with tempfile.TemporaryDirectory() as tmp:
        raw = {"claude-opus-5-5": dict(BASE, provider_specific_entry={"fast": 2})}
        saved_env = os.environ.pop("USAGE_CLAUDE_OFFLINE")
        try:
            assert P.load(tmp, lambda: raw)[0]["claude-opus-5-5"]["_fast"] == 2.0
            def down():
                raise OSError("offline")
            pr, off = P.load(tmp, down)
        finally:
            os.environ["USAGE_CLAUDE_OFFLINE"] = saved_env
        assert off and pr["claude-opus-5-5"]["_fast"] == 2.0
    ship = P._read_list(os.path.join(P.HERE, "prices.json"))
    assert ship["claude-opus-5-5"]["_fast"] == 2.0 and ship["claude-fable-5-2"]["_fast"] == 2.0
    assert ship["claude-opus-4-6"]["_fast"] == 6.0 and "_fast" not in ship["claude-sonnet-5-5"]


def test_find():
    assert P.find(PRICES, "claude-a") is PRICES["claude-a"]
    assert P.find(PRICES, "claude-a-20260101") is PRICES["claude-a"]            # dated form
    assert P.find(PRICES, "anthropic/claude-a") is PRICES["claude-a"]           # provider prefix
    assert P.find(PRICES, "us.anthropic.claude-a-v1:0") is PRICES["claude-a"]
    assert P.find(PRICES, "anthropic.claude-a-20260101-v1:0") is PRICES["claude-a"]
    exact = P.sanitize({"claude-a-20260101": dict(BASE, input_cost_per_token=3e-6), "claude-a": BASE})
    assert P.find(exact, "claude-a-20260101")["input_cost_per_token"] == 3e-6   # the exact id first
    assert P.find(PRICES, "claude-zzz") is None and P.find(PRICES, "claude-a-x") is None
    assert P.find(PRICES, None) is None and P.find(PRICES, 5) is None and P.find({}, "claude-a") is None


def deep(n):
    d = x = {}
    for _ in range(n):
        x["k"] = {}
        x = x["k"]
    return d


def test_garbage_price_list():
    bad = {
        "claude-str": dict(BASE, input_cost_per_token="1e-6"),
        "claude-neg": dict(BASE, output_cost_per_token=-1e-6),
        "claude-nan": dict(BASE, cache_read_input_token_cost=float("nan")),
        "claude-inf": dict(BASE, input_cost_per_token=float("inf")),
        "claude-huge": dict(BASE, input_cost_per_token=1e9),
        "claude-bool": dict(BASE, input_cost_per_token=True),
        "claude-none": dict(BASE, input_cost_per_token=None),
        "claude-missing": {"input_cost_per_token": 1e-6},
        "claude-list": [1, 2],
        "claude-int": 5,
        7: dict(BASE),
        "claude-good": dict(BASE, junk=deep(5000), search_context_cost_per_query={"a": [1]},
                            input_cost_per_token_above_xk_tokens=1.0, input_cost_per_token_above_100k_tokens="no",
                            output_cost_per_token_above_200k_tokens=-1, cache_read_input_token_cost_above_1000000000k_tokens=1e-7),
    }
    out = P.sanitize(bad)
    assert list(out) == ["claude-good"], list(out)
    assert out["claude-good"]["_tiers"] == [] and set(out["claude-good"]) == set(BASE) | {"_tiers"}
    for junk in (None, [], "x", 5, {"a": 1}, deep(5000)):
        assert P.sanitize(junk) == {}
    for e in out.values():
        assert all(math.isfinite(v) and v >= 0 for k, v in e.items() if k != "_tiers")


def test_load_and_fallbacks():
    shipped = P.sanitize(json.load(open(os.path.join(APP, "prices.json")))["models"])
    assert "claude-sonnet-5-5" in shipped and "claude-fable-5-2" in shipped
    with tempfile.TemporaryDirectory() as tmp:
        cache = os.path.join(tmp, "cache")
        calls = []

        def good():
            calls.append(1)
            return dict(RAW, **{"claude-new": BASE})

        def boom():
            calls.append(1)
            raise OSError("no network")

        # offline switch: no request at all, the shipped list is used
        prices, offline = P.load(cache, good)
        assert offline is True and calls == [] and prices == shipped and not os.path.exists(cache)
        del os.environ["USAGE_CLAUDE_OFFLINE"]
        try:
            prices, offline = P.load(cache, good)                  # fetched and stored
            assert offline is False and "claude-new" in prices and "gpt-x" not in prices and calls == [1]
            stored = json.load(open(os.path.join(cache, "prices.json")))["models"]
            assert "claude-new" in stored and "gpt-x" not in stored
            prices, offline = P.load(cache, boom)                  # failure: the stored copy
            assert offline is True and "claude-new" in prices
            prices, offline = P.load(cache, lambda: {"claude-garbage": {"input_cost_per_token": "x"}})
            assert offline is True and "claude-new" in prices      # nothing usable fetched: stored copy, not replaced
            assert "claude-new" in json.load(open(os.path.join(cache, "prices.json")))["models"]
            for bad in ("not a dict", deep(100000), None):
                prices, offline = P.load(cache, lambda b=bad: b)
                assert offline is True and "claude-new" in prices
            with open(os.path.join(cache, "prices.json"), "w") as fh:
                fh.write("{broken")                                # broken stored copy: the shipped one
            prices, offline = P.load(cache, boom)
            assert offline is True and prices == shipped
        finally:
            os.environ["USAGE_CLAUDE_OFFLINE"] = "1"


def test_fetch_limits():
    body = {"payload": b'{"claude-a": {}}'}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/redirect":
                self.send_response(302)
                self.send_header("Location", "http://localhost:%d/ok" % self.server.server_port)
                self.end_headers()
                return
            data = body["payload"]
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % srv.server_port
    try:
        assert P.fetch(base + "/ok") == {"claude-a": {}}
        body["payload"] = b" " * (P.MAX_BYTES + 1)
        try:
            P.fetch(base + "/ok")
            raise AssertionError("an oversize list must be refused")
        except ValueError:
            pass
        try:
            P.fetch(base + "/redirect")                            # another host name: not followed
            raise AssertionError("a redirect to another host must be refused")
        except urllib.error.HTTPError as e:
            assert e.code == 302
    finally:
        srv.shutdown()
        srv.server_close()
    h = P._SameHost()
    req = urllib.request.Request("https://raw.example.org/a")
    assert h.redirect_request(req, None, 302, "", {}, "https://raw.example.org/b") is not None
    assert h.redirect_request(req, None, 302, "", {}, "https://evil.example.net/b") is None
    assert h.redirect_request(req, None, 302, "", {}, "http://raw.example.org/b") is None
    assert P.URL.startswith("https://raw.githubusercontent.com/BerriAI/litellm/")


# --- build.py on made-up transcripts ----------------------------------------------------------

def line(mid, rid, ts, out, model="claude-a", usage=None, **extra):
    u = {"input_tokens": 10, "output_tokens": out, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 30,
         "cache_creation": {"ephemeral_5m_input_tokens": 20, "ephemeral_1h_input_tokens": 10}}
    u.update(usage or {})
    o = {"type": "assistant", "timestamp": ts, "uuid": "u" + mid, "requestId": rid, "cwd": "/x",
         "message": {"id": mid, "model": model, "content": [], "usage": u}}
    o.update(extra)
    return o


def write(path, lines):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as fh:
        for o in lines:
            fh.write(json.dumps(o) + "\n")


BIG = P.sanitize({"claude-a": {k: v * 1000 for k, v in BASE.items()}})  # large enough to see in 4 decimals


def run_build(root):
    return B.build(BIG, {"windows": []}, None, None, root=root)


def test_duplicates():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        # one message streamed in three lines: the first line gives the time (hour 10), the highest output the tokens
        write(os.path.join(root, "-x", "aaaaaaaa-1.jsonl"), [
            line("m1", "r1", "2026-11-10T10:59:59.000Z", 5),
            line("m1", "r1", "2026-11-10T11:00:05.000Z", 50),
            line("m1", "r1", "2026-11-10T11:00:09.000Z", 40),
            line("m2", "r2", "2026-11-10T12:00:00.000Z", 7),
        ])
        time.sleep(0.05)
        # a forked session copies m1 (caught earlier in its streaming) and m2 and adds m3: the older file keeps m1, m2
        write(os.path.join(root, "-x", "bbbbbbbb-2.jsonl"), [
            line("m1", "r1", "2026-11-10T10:59:59.000Z", 20),
            line("m2", "r2", "2026-11-10T12:00:00.000Z", 7),
            line("m3", "r3", "2026-11-10T13:00:00.000Z", 9),
            line("m4", None, "2026-11-10T14:00:00.000Z", 3),   # no ids: counted as it is
            line("m4", None, "2026-11-10T14:00:01.000Z", 4),
        ])
        out = run_build(root)
    rows = {r[0]: r for r in out["facts"]}
    assert sorted(rows) == ["2026-11-10T10:00:00Z", "2026-11-10T12:00:00Z", "2026-11-10T13:00:00Z",
                            "2026-11-10T14:00:00Z"], sorted(rows)
    assert 11 not in [int(h[11:13]) for h in rows], "the later lines must not move the message to hour 11"
    nv = B.NV
    r = rows["2026-11-10T10:00:00Z"]
    assert r[nv:nv + 4] == [10, 50, 30, 100] and r[-1] == 1, r        # last/highest output; cache write 20 + 10
    p = BIG["claude-a"]
    assert abs(r[nv + 4] - P.message_cost(p, 10, 50, 20, 10, 100)) < 1e-3 and r[nv + 4] > 0.1   # rows hold 4 decimals
    assert abs(r[nv + 5] - P.cache_saved(p, 10, 20, 10, 100)) < 1e-3 and r[nv + 5] > 0.01
    assert rows["2026-11-10T14:00:00Z"][-1] == 2                       # both id-less lines are counted
    assert sum(r[-1] for r in out["facts"]) == 5 and out["meta"][0]["check"]["unpriced_models"] == []
    msgs = {s[0]: s[5] for s in out["sessions"]}
    assert msgs == {"aaaaaaaa": 2, "bbbbbbbb": 3}, msgs               # m1, m2 for the original; m3, m4 x2 for the fork


def test_unpriced_and_approx():
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        write(os.path.join(root, "-x", "aaaaaaaa-1.jsonl"), [
            line("m1", "r1", "2026-11-10T10:00:00.000Z", 5),
            line("m2", "r2", "2026-11-10T10:10:00.000Z", 5, model="claude-nobody-9"),
            line("m3", "r3", "2026-11-10T10:20:00.000Z", 5, usage={"speed": "turbo"}),    # ccusage drops this line
            line("m4", "r4", "2026-11-10T10:30:00.000Z", 5, costUSD=None),                 # and this one
            line("m5", "r5", "2026-11-10T10:40:00.000Z", 5, costUSD="7.5"),                # and this one
            line("m6", "r6", "2026-11-10T10:50:00.000Z", 5, usage={"speed": "fast"}),     # a rule now: no fast price here
            line("m7", "r7", "2026-11-10T10:55:00.000Z", 5, usage={"inference_geo": "us"}),   # a rule now: no surcharge on claude-a
            line("m8", "r8", "2026-11-10T10:56:00.000Z", 5, costUSD=0.5),                  # a rule now: exactly 0.5
            line("m3", "r3", "2026-11-10T10:21:00.000Z", 6, usage={"speed": "turbo"}),    # a rewrite is not a second call
        ])
        out = run_build(root)
    chk = out["meta"][0]["check"]
    assert chk["unpriced_models"] == ["nobody-9"], chk
    assert chk["approx_pricing"] == 3, chk                               # unknown speed, costUSD null, costUSD string
    by = {r[1]: r for r in out["facts"]}
    assert by["nobody-9"][B.NV + 4] == 0 and by["nobody-9"][B.NV + 1] == 5   # tokens kept, cost 0, and reported
    std = P.message_cost(BIG["claude-a"], 10, 5, 20, 10, 100)
    # m1, m4..m7 at the computed price (m3 with 6 output tokens: its second line has more), m8 at its costUSD
    expect = 5 * std + P.message_cost(BIG["claude-a"], 10, 6, 20, 10, 100) + 0.5
    assert abs(by["a"][B.NV + 4] - expect) < 1e-3 and expect > 0.1, (by["a"][B.NV + 4], expect)


def tl(mid, ts, model="claude-sonnet-5-5", i=0, o=0, cr=0, c5=0, c1=0, usage=None, **extra):
    """A call with exactly these tokens (the probe.py shape): message mid, request r-mid."""
    u = {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr, "cache_creation_input_tokens": c5 + c1,
         "cache_creation": {"ephemeral_5m_input_tokens": c5, "ephemeral_1h_input_tokens": c1}}
    u.update(usage or {})
    o_ = {"type": "assistant", "timestamp": ts, "uuid": "u" + mid + ts, "requestId": "r-" + mid, "cwd": "/x",
          "message": {"id": mid, "model": model, "content": [], "usage": u}}
    o_.update(extra)
    return o_


def one(lines, model=None):
    """(input, output, cache write, cache read, cost, unpriced models) summed over the facts of the made-up lines."""
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        write(os.path.join(root, "-x", "aaaaaaaa-1.jsonl"), lines)
        out = B.build(CC, {"windows": []}, None, None, root=root)
    rows = [r for r in out["facts"] if model is None or r[1] == model]
    nv = B.NV
    return tuple(sum(r[nv + k] for r in rows) for k in range(5)) + (out["meta"][0]["check"]["unpriced_models"],)


T0, T1 = "2026-11-10T10:00:00.000Z", "2026-11-10T10:00:01.000Z"
STDTOK = dict(i=1000, o=1000, c5=1000, c1=1000, cr=1000)


def check(got, tokens, cost):
    assert got[:4] == tokens and abs(got[4] - cost) < 5e-5, (got, tokens, cost)


def test_duplicate_winner():
    # case 6: of the lines of one message.id + requestId the one with the most tokens counts, whatever its place
    # (probe.py dup_* and dq_* cases; the first number is input, then output, cache write, cache read)
    pairs = [
        ((dict(i=1000, o=100), dict(i=2000, o=50)), (2000, 50, 0, 0), 0.0045),                 # dup_first_is_highest
        ((dict(i=10000, o=1), dict(i=1000, o=5000)), (10000, 1, 0, 0), 0.02001),               # dq_in_vs_out: not the highest output
        ((dict(cr=100000, i=1), dict(i=20000)), (1, 0, 0, 100000), 0.0100002),                 # dq_read_vs_in: the cache read counts
        ((dict(c5=50000, i=1), dict(i=30000)), (1, 0, 50000, 0), 0.125002),                    # dq_write_vs_in
        ((dict(o=3000), dict(i=1500)), (0, 3000, 0, 0), 0.03),                                 # dq_out_vs_in
        ((dict(i=1000, o=100), dict(i=900, o=200)), (900, 200, 0, 0), 0.0038),                 # tie_sum: same total, higher cost
    ]
    for (a, b), tokens, cost in pairs:
        for order in ((a, b), (b, a)):                                  # both orders give the same winner
            check(one([tl("m", T0, **order[0]), tl("m", T1, **order[1])]), tokens, cost)
    # three lines, any order: the largest total
    check(one([tl("m", T0, i=1000, o=10), tl("m", T1, i=2000, o=100), tl("m", "2026-11-10T10:00:02.000Z", i=3000, o=50)]),
          (3000, 50, 0, 0), 0.0065)                                      # dup_three
    # a tie in tokens: the higher cost, and a costUSD counts as the cost (dup_cost_*, tie_costusd_*)
    full = (1000, 1000, 2000, 1000)
    check(one([tl("m", T0, costUSD=0.0001, **STDTOK), tl("m", T1, **STDTOK)]), full, 0.0186)   # computed 0.0186 beats 0.0001
    check(one([tl("m", T0, **STDTOK), tl("m", T1, costUSD=0.0001, **STDTOK)]), full, 0.0186)
    check(one([tl("m", T0, costUSD=1.0, **STDTOK), tl("m", T1, costUSD=2.0, **STDTOK)]), full, 2.0)
    check(one([tl("m", T0, costUSD=2.0, **STDTOK), tl("m", T1, costUSD=1.0, **STDTOK)]), full, 2.0)
    check(one([tl("m", T0, costUSD=5.0, i=1000), tl("m", T1, i=2000)]), (2000, 0, 0, 0), 0.004)   # more tokens beats a bigger costUSD
    check(one([tl("m", T0, costUSD=5.0, i=2000), tl("m", T1, i=1000)]), (2000, 0, 0, 0), 5.0)
    # a fast line costs more than the same tokens at standard speed, so it wins the tie (speed_O_fast_cost_dup[_rev])
    fast = {"speed": "fast"}
    opus = dict(model="claude-opus-5-5", **STDTOK)
    check(one([tl("m", T0, usage=fast, **opus), tl("m", T1, **opus)]), full, 0.0744)
    check(one([tl("m", T0, **opus), tl("m", T1, usage=fast, **opus)]), full, 0.0744)
    # more tokens at standard speed beats a fast line with fewer (dup_fast_then_standard: 1000 in, 100 out, 0.006)
    check(one([tl("m", T0, usage=fast, model="claude-opus-5-5", i=1000, o=10), tl("m", T1, model="claude-opus-5-5", i=1000, o=100)]),
          (1000, 100, 0, 0), 0.006)
    # lines without both ids are separate calls (dup_no_ids, dup_mid_only, dup_rid_only)
    a, b = tl("m", T0, i=1000, o=100), tl("m", T1, i=2000, o=50)
    for k in (("id", "requestId"), ("requestId",), ("id",)):
        x, y = json.loads(json.dumps(a)), json.loads(json.dumps(b))
        for z in (x, y):
            for f in k:
                (z["message"] if f == "id" else z).pop(f)
        check(one([x, y]), (3000, 150, 0, 0), 0.0075)


def test_cost_usd_speed_geo():
    # case 5: a call with a numeric costUSD costs exactly that, also zero, negative or tiny, also for a model with no price
    for cu in (123.456, 0, -5.0, 5, 1e-9):
        check(one([tl("m", T0, costUSD=cu, **STDTOK)]), (1000, 1000, 2000, 1000), float(cu))   # cost_odd, _zero, _negative, _int, _tiny
    got = one([tl("m", T0, model="claude-nobody-9", costUSD=5.0, **STDTOK)])                # cost_unpriced_model
    check(got, (1000, 1000, 2000, 1000), 5.0)
    assert got[5] == [], "a call with its own costUSD is not an unpriced model"
    check(one([tl("m1", T0, costUSD=10.0, **STDTOK), tl("m2", T1, **STDTOK)]), (2000, 2000, 4000, 2000), 10.0186)  # cost_two_msgs
    inner = tl("m", T0, **STDTOK)
    inner["message"]["costUSD"] = 123.456                                   # cost_in_message: only the entry's own field counts
    check(one([inner]), (1000, 1000, 2000, 1000), 0.0186)
    # costUSD set on a fast call: the costUSD (cost_fast, 3.0)
    check(one([tl("m", T0, model="claude-opus-5-5", costUSD=3.0, usage={"speed": "fast"}, **STDTOK)]), (1000, 1000, 2000, 1000), 3.0)
    # case 2: ccusage ignores inference_geo (geo_*_us equals geo_*_global for every model); the app does not, for "us"
    # only, see test_us_geo. Every other value is priced as before.
    for model, c in (("claude-sonnet-5-5", 0.0186), ("claude-opus-5-5", 0.0372), ("claude-haiku-5-5", 0.000935),
                     ("claude-fable-5-2", 0.0742)):
        for geo in ("global", "eu", "US", "", "not_available", None):
            check(one([tl("m", T0, model=model, usage={"inference_geo": geo}, **STDTOK)]), (1000, 1000, 2000, 1000), c)
    # case 1 through the build: fast multiplies, the model name stays
    check(one([tl("m", T0, model="claude-opus-5-5", usage={"speed": "fast"}, **STDTOK)]), (1000, 1000, 2000, 1000), 0.0744)
    check(one([tl("m", T0, model="claude-opus-4-6", usage={"speed": "fast"}, **STDTOK)]), (1000, 1000, 2000, 1000), 0.2805)
    check(one([tl("m", T0, model="claude-sonnet-5-5", usage={"speed": "fast"}, **STDTOK)]), (1000, 1000, 2000, 1000), 0.0186)


def test_us_geo():
    # Anthropic's data residency pricing (decided 11 Nov 2026: where ccusage and Anthropic differ, follow Anthropic):
    # inference_geo exactly "us" costs 1.1x on every token type for Opus 4.6, Sonnet 4.6 and every later model
    us = lambda model, **u: one([tl("m", T0, model=model, usage=dict(inference_geo="us", **u), **STDTOK)])
    glob = lambda model, **u: one([tl("m", T0, model=model, usage=dict(inference_geo="global", **u), **STDTOK)])
    for model, c in (("claude-sonnet-5-5", 0.0186), ("claude-opus-5-5", 0.0372), ("claude-fable-5-2", 0.0742)):
        check(glob(model), (1000, 1000, 2000, 1000), c)
        check(us(model), (1000, 1000, 2000, 1000), c * 1.1)                 # every token type, so the sum x 1.1
    cc("claude-opus-4-6", 0.04675, **STD)
    assert close(P.message_cost(CC["claude-opus-4-6"], 1000, 1000, 1000, 1000, 1000, us=True), 0.04675 * 1.1)
    # the haiku-5-5 tier price is multiplied too (100001 input: the whole message at the tier price)
    check(one([tl("m", T0, model=H55, usage={"inference_geo": "us"}, i=100001, o=1000)]), (100001, 1000, 0, 0), 0.0525005 * 1.1)
    # us together with fast mode: both factors multiply (opus-5-5 fast = 2, opus-4-6 fast = 6)
    check(us("claude-opus-5-5", speed="fast"), (1000, 1000, 2000, 1000), 0.0372 * 2 * 1.1)
    check(us("claude-opus-4-6", speed="fast"), (1000, 1000, 2000, 1000), 0.2805 * 1.1)
    # a model before 4.6 has no surcharge (the one old model of the list here has the Bedrock id)
    check(us(S45), (1000, 1000, 2000, 1000), glob(S45)[4])
    # which ids count: 4.6 and later, all 5.x, fable, mythos; not 3.x, 4, 4.1, 4.5 and older, and a date is no minor
    yes = ("claude-opus-4-6", "claude-opus-4-6-20260205", "claude-sonnet-4-6", "claude-opus-4-7-20260416", "claude-opus-4-8",
           "claude-opus-5", "claude-opus-5-5", "claude-sonnet-5", "claude-sonnet-5-5", "claude-haiku-5-5",
           "claude-fable-5", "claude-fable-5-2", "claude-mythos-5-1", "claude-mythos-preview")
    no = ("claude-opus-4", "claude-opus-4-20250514", "claude-opus-4-1", "claude-opus-4-1-20250805", "claude-opus-4-5",
          "claude-opus-4-5-20251101", "claude-sonnet-4", "claude-sonnet-4-20250514", "claude-sonnet-4-5",
          S45, "claude-haiku-4-5", "claude-haiku-4-5-20251001", "claude-3-5-sonnet-20241022", "claude-3-opus-20240229",
          "claude-3-haiku-20240307", "claude-3-7-sonnet-20250219")
    got = P.sanitize({m: BASE for m in yes + no})
    assert [m for m in yes + no if "_us" in got[m]] == list(yes), [m for m in yes + no if "_us" in got[m]]
    assert all(got[m]["_us"] == 1.1 for m in yes)
    # a stored copy and the shipped list keep the rule, and the cache saving follows the same factor
    ship = P._read_list(os.path.join(P.HERE, "prices.json"))
    assert ship["claude-sonnet-5-5"]["_us"] == 1.1 and "_us" not in ship["claude-opus-4-5"] and "_us" not in ship["claude-haiku-4-5"]
    assert close(P.cache_saved(CC["claude-opus-5-5"], 0, 0, 0, 1000, True, True), 1.1 * 2 * 1000 * (4e-6 - 2e-7))
    assert close(P.message_cost(CC["claude-opus-5-5"], 1000, 0, 0, 0, 0, us=True), 1.1 * 4e-3)
    # a call with its own costUSD costs exactly that, also with "us"; the count of "us" calls is in meta.check
    check(one([tl("m", T0, usage={"inference_geo": "us"}, costUSD=2.0, **STDTOK)]), (1000, 1000, 2000, 1000), 2.0)
    with tempfile.TemporaryDirectory() as tmp:
        root = os.path.join(tmp, "projects")
        write(os.path.join(root, "-x", "aaaaaaaa-1.jsonl"), [
            tl("m1", T0, usage={"inference_geo": "us"}, **STDTOK), tl("m2", T0, usage={"inference_geo": "global"}, **STDTOK),
            tl("m3", T0, model=S45, usage={"inference_geo": "us"}, **STDTOK), tl("m4", T0, **STDTOK),
            tl("m1", T1, usage={"inference_geo": "us"}, **STDTOK)])     # a rewrite of m1 is not a second call
        chk = B.build(CC, {"windows": []}, None, None, root=root)["meta"][0]["check"]
    assert chk["us_geo_calls"] == 2 and chk["skipped_files"] == 0, chk


def main():
    for t in (test_base_and_cache_split, test_tiers, test_ccusage_rules, test_fast_mode, test_find,
              test_garbage_price_list, test_load_and_fallbacks, test_fetch_limits, test_duplicates,
              test_unpriced_and_approx, test_duplicate_winner, test_cost_usd_speed_geo, test_us_geo):
        t()
    print("ok: base prices, 5m/1h writes, tiers (200k split per type, 100k whole, threshold), fast mode, costUSD, "
          "inference_geo (1.1x for \"us\" on 4.6 and later, alone and with fast mode), model lookup, garbage price list, load fallbacks, fetch limits, duplicate winner, "
          "unpriced and approx_pricing")


if __name__ == "__main__":
    main()
