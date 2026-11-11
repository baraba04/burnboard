#!/usr/bin/env python3
"""Developer check, not part of the app: the app's own cost against `ccusage daily` per (local day, model), both
taken now from the active profile's real transcripts. It downloads ccusage from npm (needs npx) and prints
only counts and totals as JSON:
  cells, within_cent, max_abs_diff, total_ccusage, total_own, us_geo_calls
us_geo_calls is the number of calls with inference_geo "us"; the app prices them at Anthropic's 1.1x where it applies
and ccusage does not, so a gap in the totals can be explained by it (research/pricing/FINDINGS.md, case 2).
Exit code 0, or 1 with {"error": "<code>"}.   Run: python3 tests/compare_ccusage.py
"""
import json
import os
import shutil
import signal
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import build as B  # noqa: E402
import core  # noqa: E402
import pricing  # noqa: E402

CCUSAGE = ["npx", "-y", "ccusage@20.0.48", "daily", "--breakdown", "--json"]


def compare():
    """The result dict, or {"error": code}."""
    profile = core.active_profile(core.load_config())
    if not shutil.which("npx", path=core.env(profile)["PATH"]):
        return {"error": "no_node"}
    signal.signal(signal.SIGALRM, core.cut_off)
    signal.signal(signal.SIGTERM, core.cut_off)
    signal.alarm(300)
    cc = core.spawn(CCUSAGE, profile)
    prices, _ = pricing.load(core.cache_root())
    roots, groups = core.naming(core.load_config())
    srcs = core.read_roots(profile)
    out = B.build(prices, {"windows": []}, os.path.join(core.cache_dir(profile), "parsed.pickle"), None,
                  root=srcs[0][1], project_roots=roots, group_folders=groups, extra=srcs[1:])
    try:
        daily = json.loads(cc.communicate()[0])["daily"]
    except (ValueError, KeyError, TypeError):
        return {"error": "ccusage_failed"}
    theirs, mine = {}, {}
    for d in daily:
        for m in d["modelBreakdowns"]:
            theirs[(d["period"], m["modelName"].replace("claude-", ""))] = m["cost"]
    for r in out["facts"]:
        k = (datetime.fromisoformat(r[0].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d"), r[1])
        mine[k] = mine.get(k, 0.0) + r[B.NV + 4]
    diffs = [abs(theirs.get(k, 0.0) - mine.get(k, 0.0)) for k in set(theirs) | set(mine)]
    return {"cells": len(diffs), "within_cent": sum(1 for x in diffs if x <= 0.01),
            "max_abs_diff": round(max(diffs, default=0.0), 4),
            "total_ccusage": round(sum(theirs.values()), 4), "total_own": round(sum(mine.values()), 4),
            "us_geo_calls": out["meta"][0]["check"]["us_geo_calls"]}


if __name__ == "__main__":
    os.umask(0o077)
    res = compare()
    print(json.dumps(res))
    sys.exit(1 if "error" in res else 0)
