"""Re-score every experiments/results/*.json from its raw events (so scoring changes
apply to old runs) and print one comparison table.

  python3 experiments/summarize.py [--md]
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import harness_lib as H  # noqa: E402
from multiturn import PRESETS, read_wav  # noqa: E402

RES = Path(__file__).resolve().parent / "results"


def schedule_for(name, res):
    sch = res.get("schedule")
    if sch:
        clips, gap = sch["clips"], sch["gap"]
    else:  # older results: infer from the condition name, e.g. gated_conv_g5
        m = re.search(r"_(conv4|conv|hard)_g(\d+)$", name)
        clips, gap = PRESETS[m.group(1)], float(m.group(2))
        if name.startswith("gated"):
            gap = max(gap, 4.0)
    specs = H.load_clip_specs()
    return H.build_schedule(
        [(n, specs[n][1], specs[n][0], read_wav(H.CLIPS_DIR / f"{n}.wav")) for n in clips], gap)


def main():
    md = "--md" in sys.argv
    cols = ["runs", "in_recall", "in_tail_drop", "OUT_tail_drop", "empty", "late",
            "spurious", "first_audio", "last_out", "closes"]
    rows = []
    groups = {}
    for f in sorted(RES.glob("*.json")):
        groups.setdefault(f.stem.split("__")[0], []).append(f)
    for stem, files in groups.items():
        runs = [r for f in files for r in json.loads(f.read_text())]
        segs = schedule_for(stem, runs[0])
        aggs = []
        for r in runs:
            evs = [H.Event(**e) for e in r["events"]]
            aggs.append(H.score(evs, segs)["agg"])
        n = len(aggs)
        tot = lambda k: sum(a[k] for a in aggs)
        med = lambda k: H._median([a[k] for a in aggs if a[k] is not None])
        rows.append([stem, n, round(tot("mean_input_recall") / n, 3),
                     f"{tot('tails_dropped')}/{n * aggs[0]['clips']}",
                     f"{tot('out_tails_dropped')}/{n * aggs[0]['clips']}",
                     tot("empty_outputs"), tot("late_out_chars"), tot("spurious_out_chars"),
                     med("median_first_audio_s"), med("median_last_out_after_end_s"), tot("closes")])
    head = ["condition"] + cols
    if md:
        print("| " + " | ".join(head) + " |")
        print("|" + "---|" * len(head))
        for r in rows:
            print("| " + " | ".join(str(x) for x in r) + " |")
    else:
        w = [max(len(str(x)) for x in col) for col in zip(head, *rows)]
        for r in [head] + rows:
            print("  ".join(str(x).ljust(k) for x, k in zip(r, w)))


if __name__ == "__main__":
    main()
