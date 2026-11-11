#!/usr/bin/env python3
"""Black-box probe of ccusage 20.0.48: made-up transcripts, one per case, one local day per case, then the cost
ccusage reports for each. Nothing real is read. Usage: probe.py <empty-temp-folder> [--offline] [--cases a,b]
It writes <folder>/projects/-probe/<case>.jsonl, runs `npx ccusage@20.0.48 daily --breakdown --json -z UTC`
with CLAUDE_CONFIG_DIR=<folder>, and prints one row per (case, model): tokens and cost.
Results of the run of 11 Nov 2026 are in FINDINGS.md."""
import json
import os
import subprocess
import sys
from datetime import date, timedelta

CASES = {}   # name -> (list of file bodies; a file body is a list of entries)


def ln(mid, rid, model, i=0, o=0, cr=0, c5=0, c1=0, usage=None, ts_min=0, day_off=0, **extra):
    u = {"input_tokens": i, "output_tokens": o, "cache_read_input_tokens": cr,
         "cache_creation_input_tokens": c5 + c1,
         "cache_creation": {"ephemeral_5m_input_tokens": c5, "ephemeral_1h_input_tokens": c1},
         "service_tier": "standard", "inference_geo": "global", "speed": "standard"}
    u.update(usage or {})
    u = {k: v for k, v in u.items() if v != "__del__"}   # "__del__" removes a key
    msg = {"id": mid, "role": "assistant", "type": "message", "model": model, "content": [], "usage": u}
    o = {"type": "assistant", "sessionId": "00000000-0000-4000-8000-000000000000", "cwd": "/probe",
         "timestamp": "@%d:%d" % (ts_min, day_off), "uuid": "u-" + str(mid) + "-" + str(ts_min), "message": msg}
    if rid is not None:
        o["requestId"] = rid
    if mid is None:
        del msg["id"]
    o.update(extra)
    return o


def case(name, *files):
    CASES[name] = list(files)


M = dict(S45="claude-sonnet-4-5-20250929-v1:0", H="claude-haiku-5-5", S="claude-sonnet-5-5", O="claude-opus-5-5",
         F="claude-fable-5-2")
# --- tiers: one case per size; the threshold test uses input tokens only so the price shows in the cost
for tag, model, thr in (("s45", M["S45"], 200000), ("h55", M["H"], 100000)):
    for d in (-1, 0, 1):
        case("tier_%s_in_%+d" % (tag, d), [ln("m", "r", model, i=thr + d)])
    # which fields count toward the total: each alone at thr+1, and a split of the same size
    case("tier_%s_cr_+1" % tag, [ln("m", "r", model, cr=thr + 1)])
    case("tier_%s_c5_+1" % tag, [ln("m", "r", model, c5=thr + 1)])
    case("tier_%s_c1_+1" % tag, [ln("m", "r", model, c1=thr + 1)])
    case("tier_%s_out_+1" % tag, [ln("m", "r", model, i=10, o=thr + 1)])
    case("tier_%s_split_+1" % tag, [ln("m", "r", model, i=thr // 4, c5=thr // 4, c1=thr // 4, cr=thr // 4 + 1)])
    case("tier_%s_split_0" % tag, [ln("m", "r", model, i=thr // 4, c5=thr // 4, c1=thr // 4, cr=thr // 4)])
    # a big message with every field set: is the whole message at the tier price, or only the part above?
    case("tier_%s_whole" % tag, [ln("m", "r", model, i=thr // 2, o=1000, c5=1000, c1=1000, cr=thr // 2 + 1)])
    case("tier_%s_whole_big" % tag, [ln("m", "r", model, i=thr, o=1000, c5=1000, c1=1000, cr=thr * 2)])
case("tier_s45_noprefix_250k", [ln("m", "r", "claude-sonnet-4-5-20250929", i=250000)])   # list key without a tier
case("tier_s45_short_250k", [ln("m", "r", "claude-sonnet-4-5", i=250000)])
case("tier_o55_250k", [ln("m", "r", M["O"], i=250000)])                                 # no tier in the list
case("tier_s55_1m", [ln("m", "r", M["S"], i=900000)])
# --- speed
for k, model in (("O", M["O"]), ("F", M["F"]), ("opus5", "claude-opus-5"), ("opus48", "claude-opus-4-8"),
                 ("myth52", "claude-mythos-5-2"), ("opus46", "claude-opus-4-6"), ("S", M["S"]), ("H", M["H"])):
    for sp in ("standard", "fast"):
        case("speed_%s_%s" % (k, sp), [ln("m", "r", model, i=1000, o=1000, c5=1000, c1=1000, cr=1000,
                                          usage={"speed": sp})])
case("speed_O_unknown", [ln("m", "r", M["O"], i=1000, o=1000, c5=1000, c1=1000, cr=1000, usage={"speed": "turbo"})])
case("speed_O_toplevel", [ln("m", "r", M["O"], i=1000, o=1000, c5=1000, c1=1000, cr=1000, speed="fast")])
case("speed_O_fast_us", [ln("m", "r", M["O"], i=1000, o=1000, c5=1000, c1=1000, cr=1000,
                            usage={"speed": "fast", "inference_geo": "us"})])
case("speed_O_fast_tier_big", [ln("m", "r", M["O"], i=250000, o=1000, usage={"speed": "fast"})])
# --- inference_geo
for k, model in (("S", M["S"]), ("O", M["O"]), ("H", M["H"]), ("F", M["F"]), ("s45short", "claude-sonnet-4-5"),
                 ("opus45", "claude-opus-4-5"), ("opus46", "claude-opus-4-6")):
    for g in ("global", "us"):
        case("geo_%s_%s" % (k, g), [ln("m", "r", model, i=1000, o=1000, c5=1000, c1=1000, cr=1000,
                                       usage={"inference_geo": g})])
for g in ("eu", "US", "not_available", ""):
    case("geo_S_%s" % (g or "empty"), [ln("m", "r", M["S"], i=1000, o=1000, c5=1000, c1=1000, cr=1000,
                                          usage={"inference_geo": g})])
case("geo_H_us_tier", [ln("m", "r", M["H"], i=100001, o=1000, usage={"inference_geo": "us"})])
case("geo_H_global_tier", [ln("m", "r", M["H"], i=100001, o=1000)])
# --- costUSD
std = dict(i=1000, o=1000, c5=1000, c1=1000, cr=1000)
case("cost_none", [ln("m", "r", M["S"], **std)])
case("cost_odd", [ln("m", "r", M["S"], costUSD=123.456, **std)])
case("cost_zero", [ln("m", "r", M["S"], costUSD=0, **std)])
case("cost_null", [ln("m", "r", M["S"], costUSD=None, **std)])
case("cost_string", [ln("m", "r", M["S"], costUSD="7.5", **std)])
case("cost_in_message", [{**ln("m", "r", M["S"], **std), "message": {**ln("m", "r", M["S"], **std)["message"],
                                                                      "costUSD": 123.456}}])
case("cost_unpriced_model", [ln("m", "r", "claude-nobody-9", costUSD=5.0, **std)])
case("cost_two_msgs", [ln("m1", "r1", M["S"], costUSD=10.0, **std), ln("m2", "r2", M["S"], **std, ts_min=1)])
# --- duplicates (same message.id and requestId); S prices: input 2e-6, output 1e-5
case("dup_first_is_highest", [[ln("m", "r", M["S"], i=1000, o=100), ln("m", "r", M["S"], i=2000, o=50, ts_min=1)]])
case("dup_last_is_highest", [[ln("m", "r", M["S"], i=2000, o=50), ln("m", "r", M["S"], i=1000, o=100, ts_min=1)]])
case("dup_tie_output", [[ln("m", "r", M["S"], i=1000, o=100), ln("m", "r", M["S"], i=2000, o=100, ts_min=1)]])
case("dup_three", [[ln("m", "r", M["S"], i=1000, o=10), ln("m", "r", M["S"], i=2000, o=100, ts_min=1),
                    ln("m", "r", M["S"], i=3000, o=50, ts_min=2)]])
case("dup_file_order_vs_ts", [[ln("m", "r", M["S"], i=1000, o=50, ts_min=5), ln("m", "r", M["S"], i=2000, o=100, ts_min=1)]])
case("dup_two_files", [[ln("m", "r", M["S"], i=1000, o=100)], [ln("m", "r", M["S"], i=2000, o=50, ts_min=1)]])
case("dup_two_files_rev", [[ln("m", "r", M["S"], i=2000, o=50, ts_min=1)], [ln("m", "r", M["S"], i=1000, o=100)]])
case("dup_no_ids", [[ln(None, None, M["S"], i=1000, o=100), ln(None, None, M["S"], i=2000, o=50, ts_min=1)]])
case("dup_mid_only", [[ln("m", None, M["S"], i=1000, o=100), ln("m", None, M["S"], i=2000, o=50, ts_min=1)]])
case("dup_rid_only", [[ln(None, "r", M["S"], i=1000, o=100), ln(None, "r", M["S"], i=2000, o=50, ts_min=1)]])
case("dup_fast_then_standard", [[ln("m", "r", M["O"], i=1000, o=10, usage={"speed": "fast"}),
                                 ln("m", "r", M["O"], i=1000, o=100, ts_min=1)]])
case("dup_cost_first", [[ln("m", "r", M["S"], i=1000, o=100, costUSD=10.0), ln("m", "r", M["S"], i=1000, o=100, ts_min=1)]])
case("dup_cost_last", [[ln("m", "r", M["S"], i=1000, o=100), ln("m", "r", M["S"], i=1000, o=100, ts_min=1, costUSD=10.0)]])

# --- speed and geo on every direct Anthropic id of the price list: one standard and one fast (or us) message per case
ALL = ("claude-haiku-4-5-20251001 claude-haiku-4-5 claude-sonnet-4-5 claude-sonnet-4-5-20250929 claude-sonnet-5 "
       "claude-sonnet-5-5 claude-sonnet-4-6 claude-sonnet-4-5-20250929-v1:0 claude-opus-4-5-20251101 claude-opus-4-5 "
       "claude-opus-4-6 claude-opus-4-6-20260205 claude-opus-4-7 claude-opus-4-7-20260416 claude-fable-5 claude-fable-5-1 "
       "claude-opus-5-5 claude-opus-5 claude-opus-4-8 claude-mythos-5 claude-mythos-5-1 claude-mythos-5-2 "
       "claude-mythos-preview claude-haiku-5-5 claude-fable-5-2").split()
for model in ALL:
    case("ratio_" + model, [ln("a", "a", model, **std), ln("b", "b", model, usage={"speed": "fast"}, ts_min=1, **std)])
    case("geo2_" + model, [ln("a", "a", model, **std), ln("b", "b", model, usage={"inference_geo": "us"}, ts_min=1, **std)])
# --- which line of a duplicate wins (sonnet-5-5: in 2e-6, out 1e-5, 5m write 2.5e-6, read 1e-7)
for tag, a, b in (("in_vs_out", dict(i=10000, o=1), dict(i=1000, o=5000)),
                  ("read_vs_in", dict(cr=100000, i=1), dict(i=20000)),
                  ("write_vs_in", dict(c5=50000, i=1), dict(i=30000)),
                  ("out_vs_in", dict(o=3000), dict(i=1500)),
                  ("read_vs_read", dict(i=1000, cr=5000), dict(i=1000, cr=9000)),
                  ("write_vs_write", dict(c5=1000), dict(c5=3000)),
                  ("out_vs_out", dict(i=1000, o=100), dict(i=1000, o=300))):
    case("dq_%s_ab" % tag, [[ln("m", "r", M["S"], **a), ln("m", "r", M["S"], ts_min=1, **b)]])
    case("dq_%s_ba" % tag, [[ln("m", "r", M["S"], **b), ln("m", "r", M["S"], ts_min=1, **a)]])
# --- costUSD variants
case("cost_negative", [ln("m", "r", M["S"], costUSD=-5.0, **std)])
case("cost_int", [ln("m", "r", M["S"], costUSD=5, **std)])
case("cost_tiny", [ln("m", "r", M["S"], costUSD=1e-9, **std)])
case("cost_fast", [ln("m", "r", M["O"], costUSD=3.0, usage={"speed": "fast"}, **std)])
# --- tiers of other sizes, on models that carry them in the list (not Claude ones): does the rule depend on N?
for tag, model, thr in (("gem200", "gemini-2.5-pro", 200000), ("gpt272", "gpt-5.6", 272000)):
    case("tier_%s_in_+0" % tag, [ln("m", "r", model, i=thr)])
    case("tier_%s_in_+1" % tag, [ln("m", "r", model, i=thr + 1)])
    case("tier_%s_cr_+1" % tag, [ln("m", "r", model, cr=thr + 1)])
    case("tier_%s_split_+1" % tag, [ln("m", "r", model, i=thr // 2, cr=thr // 2 + 1)])
    case("tier_%s_split_0" % tag, [ln("m", "r", model, i=thr // 2, cr=thr // 2)])
    case("tier_%s_out_+1" % tag, [ln("m", "r", model, i=10, o=thr + 1)])

# --- ties in a duplicate, and which day the message lands on
case("tie_sum_ab", [[ln("m", "r", M["S"], i=1000, o=100), ln("m", "r", M["S"], ts_min=1, i=900, o=200)]])
case("tie_sum_ba", [[ln("m", "r", M["S"], i=900, o=200), ln("m", "r", M["S"], ts_min=1, i=1000, o=100)]])
case("tie_costusd_ab", [[ln("m", "r", M["S"], costUSD=0.0001, **std), ln("m", "r", M["S"], ts_min=1, **std)]])
case("tie_costusd_ba", [[ln("m", "r", M["S"], **std), ln("m", "r", M["S"], ts_min=1, costUSD=0.0001, **std)]])
case("tie_costusd_two_ab", [[ln("m", "r", M["S"], costUSD=1.0, **std), ln("m", "r", M["S"], ts_min=1, costUSD=2.0, **std)]])
case("tie_costusd_two_ba", [[ln("m", "r", M["S"], costUSD=2.0, **std), ln("m", "r", M["S"], ts_min=1, costUSD=1.0, **std)]])
case("tie_costusd_big_loses", [[ln("m", "r", M["S"], costUSD=5.0, i=1000), ln("m", "r", M["S"], ts_min=1, i=2000)]])
case("tie_costusd_big_wins", [[ln("m", "r", M["S"], costUSD=5.0, i=2000), ln("m", "r", M["S"], ts_min=1, i=1000)]])
case("dayx_small_then_large", [[ln("m", "r", M["S"], i=100), ln("m", "r", M["S"], ts_min=1, day_off=1, i=5000)]])
case("dayx_large_then_small", [[ln("m", "r", M["S"], i=5000), ln("m", "r", M["S"], ts_min=1, day_off=1, i=100)]])
case("dayx_small_then_large_rev", [[ln("m", "r", M["S"], ts_min=1, day_off=1, i=5000), ln("m", "r", M["S"], i=100)]])
# --- other thresholds on non-Claude ids in the list, and odd Claude ids
for tag, model, thr in (("n32", "empiriolabs/glm-5-1", 32000), ("n128", "gemini/gemini-exp-1114", 128000),
                        ("n256", "kyma/qwen-3.6-plus", 256000), ("n512", "minimax/MiniMax-M3", 512000)):
    case("tier_%s_in_+1" % tag, [ln("m", "r", model, i=thr + 1)])
    case("tier_%s_in_0" % tag, [ln("m", "r", model, i=thr)])
    case("tier_%s_split_+1" % tag, [ln("m", "r", model, i=thr // 2, cr=thr // 2 + 1)])
for k, model in (("bedrock_us", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
                 ("bedrock_bare", "anthropic.claude-sonnet-4-5-20250929-v1:0"),
                 ("anthropic_slash", "anthropic/claude-sonnet-4-5"), ("opus46_unknown_date", "claude-opus-4-6-20260101"),
                 ("opus46_suffix", "claude-opus-4-6-foo"), ("opus49", "claude-opus-4-9"),
                 ("opus4_6_dots", "claude-opus-4.6")):
    case("name_%s" % k, [ln("a", "a", model, **std), ln("b", "b", model, usage={"speed": "fast"}, ts_min=1, **std)])

# --- 200k tier: is the count per token type? (each type below 200k, total far above)
case("tier_s45_each_below", [ln("m", "r", M["S45"], i=199999, c5=199999, cr=199999, o=199999)])
case("tier_s45_cw_split", [ln("m", "r", M["S45"], c5=100000, c1=100001)])
case("tier_s45_cw_c5_big", [ln("m", "r", M["S45"], c5=200001, c1=200001)])
case("tier_s45_fast", [ln("m", "r", M["S45"], i=200001, usage={"speed": "fast"})])

# --- absent or null speed / inference_geo
case("speed_O_missing", [ln("m", "r", M["O"], usage={"speed": "__del__"}, **std)])
case("speed_O_null", [ln("m", "r", M["O"], usage={"speed": None}, **std)])
case("geo_S_missing", [ln("m", "r", M["S"], usage={"inference_geo": "__del__"}, **std)])
case("geo_S_null", [ln("m", "r", M["S"], usage={"inference_geo": None}, **std)])
case("speed_O_fast_cost_dup", [[ln("m", "r", M["O"], usage={"speed": "fast"}, **std), ln("m", "r", M["O"], ts_min=1, **std)]])

case("speed_O_fast_cost_dup_rev", [[ln("m", "r", M["O"], **std), ln("m", "r", M["O"], ts_min=1, usage={"speed": "fast"}, **std)]])

def write(root, names):
    day0 = date(2026, 1, 1)
    plan, skipped = {}, 0
    d = os.path.join(root, "projects", "-probe")
    os.makedirs(d, exist_ok=True)
    for n, name in enumerate(names):
        day = (day0 + timedelta(days=n + skipped)).isoformat()
        plan[day] = name
        if name.startswith("dayx"):
            skipped += 1   # the case uses the next day too
        for fi, body in enumerate(CASES[name]):
            with open(os.path.join(d, "%s-%d.jsonl" % (name, fi)), "w") as fh:
                for e in body:
                    # ccusage dedupes on message.id + requestId across ALL files, so every case gets its own ids
                    if e["message"].get("id"):
                        e["message"]["id"] = name + "-" + e["message"]["id"]
                    if e.get("requestId"):
                        e["requestId"] = name + "-" + e["requestId"]
                    mins, doff = (int(x) for x in e["timestamp"][1:].split(":"))
                    e["timestamp"] = "%sT12:%02d:00.000Z" % ((date.fromisoformat(day) + timedelta(days=doff)).isoformat(), mins)
                    fh.write(json.dumps(e, separators=(",", ":")) + "\n")  # ccusage skips lines written with spaces
    return plan


def main():
    root = sys.argv[1]
    names = list(CASES)
    for a in sys.argv[2:]:
        if a.startswith("--cases="):
            names = a[8:].split(",")
    for k, v in CASES.items():   # a case is a list of file bodies; undo one level of nesting from case(name, [[...]])
        if len(v) == 1 and v[0] and isinstance(v[0][0], list):
            CASES[k] = v[0]
    plan = write(root, names)
    cmd = ["npx", "-y", "ccusage@20.0.48"] + (["claude"] if "--claude" in sys.argv else []) + [
        "daily", "--breakdown", "--json", "-z", "UTC"]
    if "--offline" in sys.argv:
        cmd.append("--offline")
    extra = [a[len("--pass="):] for a in sys.argv if a.startswith("--pass=")]
    cmd += [x for e in extra for x in e.split()]
    env = dict(os.environ, CLAUDE_CONFIG_DIR=root)
    out = subprocess.run(cmd, env=env, capture_output=True, text=True, cwd=root)
    if out.returncode:
        print(out.stderr[-2000:])
        return 1
    data = json.loads(out.stdout)
    for dd in data["daily"]:
        dd.setdefault("period", dd.get("date"))   # `ccusage claude daily` says "date", `ccusage daily` says "period"
        for m in dd["modelBreakdowns"]:
            print("%-28s %-34s in %7d out %7d cw %7d cr %7d  cost %.9f" % (
                plan.get(dd["period"], "?" + dd["period"]), m["modelName"], m["inputTokens"], m["outputTokens"],
                m["cacheCreationTokens"], m["cacheReadTokens"], m["cost"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
