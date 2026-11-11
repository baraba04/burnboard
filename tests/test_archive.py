#!/usr/bin/env python3
"""Check that a past day comes back from the archive when its transcripts shrink or vanish.

Uses made-up fact rows and a throwaway archive file; the real archive, cache and
transcripts are never touched.  Run: python3 tests/test_archive.py
"""
import json
import os
import sys
import tempfile
from collections import defaultdict
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "app"))
import build as B

def made_up_rows():
    """Fact rows for the 7 days before today and for today: a few hours, models and agents each day."""
    noon = datetime.now().astimezone().replace(hour=12, minute=0, second=0, microsecond=0)
    rows = []
    for back in range(0, 8):
        for h, (model, proj, agent, effort, via) in enumerate([("opus-5-5", "p1", "main", "high", ""),
                                                                 ("sonnet-5-5", "p1", "reviewer", "", "skill:x"),
                                                                 ("sonnet-5-5", "p2", "main", "medium", "mcp:y")]):
            hour = (noon - timedelta(days=back, hours=-h)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00Z")
            n = 1000 * (back + 1) + 100 * h
            rows.append([hour, model, proj, 0 if agent == "main" else 1, agent, effort, via,
                         n, n // 10, n // 20, n * 5, round(0.5 * (back + 1) + h, 4), round(0.2 * (back + 1), 4), 3 + h])
    return sorted(rows)


def local_day(hour):
    return B.parse_ts(hour).astimezone().strftime("%Y-%m-%d")


def day_totals(rows):
    t = defaultdict(lambda: [0, 0.0])
    for r in rows:
        d = t[local_day(r[0])]
        d[0] += sum(r[B.NV:B.NV + 4])
        d[1] += r[B.NV + 4]
    return t


def main():
    rows = made_up_rows()
    today = datetime.now().astimezone().strftime("%Y-%m-%d")
    full = day_totals(rows)
    past = sorted(d for d in full if d < today)
    assert len(past) >= 3, "need at least three past days in the snapshot"
    gone, half, kept = past[0], past[len(past) // 2], past[-1]
    logs = []
    with tempfile.TemporaryDirectory() as tmp:
        arch = os.path.join(tmp, "archive.json")

        # 1. First build: everything present, archive gets written, nothing restored.
        out, restored = B.merge_archive([list(r) for r in rows], arch, local_day, logs.append)
        assert restored == [], restored
        assert day_totals(out) == full
        archived = json.load(open(arch))
        assert today not in archived, "today must not be archived (still changing)"
        assert set(archived) == set(past)

        # 2. Transcripts cleaned up: one day gone entirely, one day lost half its rows.
        half_rows = [r for r in rows if local_day(r[0]) == half]
        drop = {id(r) for r in half_rows[: max(1, len(half_rows) // 2)]}
        shrunk = [list(r) for r in rows if local_day(r[0]) != gone and id(r) not in drop]
        before = day_totals(shrunk)
        assert gone not in before and before[half][0] < full[half][0]
        out, restored = B.merge_archive(shrunk, arch, local_day, logs.append)
        after = day_totals(out)
        assert restored == sorted([gone, half]), restored
        for d in (gone, half, kept, today):
            if d not in full:
                continue
            assert after[d][0] == full[d][0], (d, after[d][0], full[d][0])
            assert abs(after[d][1] - full[d][1]) < 0.01, (d, after[d][1], full[d][1])
        assert sum(v[0] for v in after.values()) == sum(v[0] for v in full.values())
        # restored rows land on the right local day and keep agent/effort/project
        for r in out:
            assert len(r) == len(B.FACT_COLS)
        keys = lambda rs, day: sorted({(r[1], r[2], r[4], r[5], r[6]) for r in rs if local_day(r[0]) == day})
        assert keys(out, gone) == keys(rows, gone)

        # 3. The archive itself must not be overwritten by the smaller day.
        again = json.load(open(arch))
        assert again[half] == archived[half] and again[gone] == archived[gone]

        # 5. An archive written before the "via" column existed (11 fields a row) is still read.
        old = {d: [x[:4] + x[5:] for x in v] for d, v in again.items()}
        old_arch = os.path.join(tmp, "old.json")
        json.dump(old, open(old_arch, "w"))
        out3, restored3 = B.merge_archive(shrunk, old_arch, local_day, logs.append)
        after3 = day_totals(out3)
        assert len(restored3) == 2 and after3[gone][0] == full[gone][0] and abs(after3[gone][1] - full[gone][1]) < 0.01
        assert all(len(x) == 12 for v in json.load(open(old_arch)).values() for x in v)

        # 4. A third build on the same shrunk input gives the same answer.
        out2, restored2 = B.merge_archive(shrunk, arch, local_day, logs.append)
        assert len(restored2) == 2 and day_totals(out2) == after

        # 6. Own pricing: a day whose transcripts still exist comes back with more output tokens (the last line of a
        #    streamed message) and another cost. It is rewritten in the archive, not restored; a day whose
        #    transcripts are gone is still restored from the archive.
        fresh_arch = os.path.join(tmp, "fresh.json")
        json.dump(archived, open(fresh_arch, "w"))
        grown, n_kept = [], 0
        for r in rows:
            if local_day(r[0]) == gone:
                continue
            r = list(r)
            if local_day(r[0]) == kept:
                r[B.NV + 1] += 50
                r[B.NV + 4] = round(r[B.NV + 4] * 1.3, 4)
                n_kept += 1
            grown.append(r)
        out6, restored6 = B.merge_archive(grown, fresh_arch, local_day, logs.append)
        assert restored6 == [gone], restored6
        a6 = json.load(open(fresh_arch))
        assert a6[gone] == archived[gone]
        assert sum(x[6] for x in a6[kept]) == sum(x[6] for x in archived[kept]) + 50 * n_kept
        assert abs(sum(x[9] for x in a6[kept]) - 1.3 * sum(x[9] for x in archived[kept])) < 0.001
        assert day_totals(out6)[kept] == day_totals(grown)[kept] and day_totals(out6)[gone] == full[gone]

        # 7. A damaged archive never stops the build and is never overwritten without a copy. A valid one makes no copy.
        assert [f for f in os.listdir(tmp) if ".corrupt-" in f] == []

        def damaged(content, name, built):
            path = os.path.join(tmp, name)
            with open(path, "w") as fh:
                fh.write(content)
            res = B.merge_archive([list(r) for r in built], path, local_day, logs.append)
            return res, path, [f for f in os.listdir(tmp) if f.startswith(name + ".corrupt-")]

        for i, text in enumerate(("[1, 2]", '"text"', "5", "not json {", "[" * 100000 + "]" * 100000, "")):
            (out7, restored7), path, copies = damaged(text, "bad%d.json" % i, rows)
            assert restored7 == [] and day_totals(out7) == full, text[:12]
            assert len(copies) == 1 and open(os.path.join(tmp, copies[0])).read() == text, text[:12]   # the damaged file is kept
            assert set(json.load(open(path))) == set(past)                                              # and a clean one written
        row = ["m", "p", "main", "", "", 1, 2, 3, 4, 0.5, 0.1, 1]
        arch7 = dict(archived)
        arch7[gone] = "oops"                                          # not a list
        arch7["2026-13-45"] = [row]                                   # not a day
        arch7["x"] = [row]
        arch7[past[1]] = [row[:5] + ["10"] + row[6:]]                 # a count that is a string
        arch7[past[2]] = [row[:3]]                                    # a row of the wrong length
        arch7[past[-2]] = [row[:9] + [float("nan")] + row[10:]]       # a cost that is not a number
        (out7, restored7), path, copies = damaged(json.dumps(arch7), "mixed.json", shrunk)
        # every damaged day is ignored (so not restored), the good ones still are; the build does not stop
        assert half not in (past[1], past[2], past[-2]) and restored7 == [half], restored7
        assert len(copies) == 1 and json.load(open(os.path.join(tmp, copies[0]))) == json.loads(json.dumps(arch7))
        assert all(B.real_day(d) for d in json.load(open(path)))
        # a damaged archive that does not need rewriting is left untouched, no copy
        (out8, _r), path8, copies8 = damaged("[1]", "nochange.json", [])
        assert copies8 == [] and open(path8).read() == "[1]" and out8 == []

    print(f"ok: {len(past)} past days archived; day {gone} (gone) and day {half} (half gone) restored; "
          f"tokens {sum(v[0] for v in full.values()):,} and cost {sum(v[1] for v in full.values()):.2f} unchanged; "
          "damaged archives (wrong top level, bad days and rows) ignored and copied before a rewrite")


if __name__ == "__main__":
    main()
