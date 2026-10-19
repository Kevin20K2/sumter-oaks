"""Compare model-trial arms: cost, rule-following, action mix, dialogue, and the day's story side by side.
Usage: python tools/trial_report.py trial/<model-a> trial/<model-b> ...   (writes Markdown to stdout)"""
import json, re, sys
from collections import Counter
from pathlib import Path


def new_ticks(arm):
    base = Path(arm)
    start = json.load(open("data/state.json"))["tick"] if Path("data/state.json").exists() else 0
    out = []
    for f in sorted((base / "data/ticks").glob("tick_*.json")):
        d = json.load(open(f))
        if d["tick"] >= start:
            out.append(d)
    return out


def norm(s):
    return re.sub(r"[^a-z ]", "", s.lower()).strip()


def stats(ticks, model):
    acts, notes, lines, retries, cost = Counter(), 0, [], 0, 0.0
    zones = {}
    for t in ticks:
        cost += t.get("cost", {}).get("usd", 0) or 0
        calls = (t.get("cost", {}).get("by_model", {}).get(model) or {}).get("calls", 0)
        retries += max(0, calls - len(t["results"]))
        for a, r in t["results"].items():
            act = r.get("action", {})
            acts[act.get("type", "?")] += 1
            if "engine_note" in r:
                notes += 1
            if act.get("type") == "SPEAK":
                lines.append((a, act.get("parameters", {}).get("content", "")))
        for a, z in t["positions"].items():
            zones.setdefault(a, set()).add(z)
    said = Counter((a, norm(l)) for a, l in lines)
    repeats = sum(n - 1 for n in said.values() if n > 1)
    words = [len(l.split()) for _, l in lines]
    return {
        "ticks": len(ticks), "cost": cost, "per_tick": cost / max(1, len(ticks)),
        "retries": retries, "notes": notes, "acts": acts, "lines": len(lines),
        "avg_words": sum(words) / max(1, len(words)), "repeats": repeats,
        "zones": sum(len(v) for v in zones.values()) / max(1, len(zones)),
    }


arms = sys.argv[1:]
data = {arm: new_ticks(arm) for arm in arms}
names = [Path(a).name for a in arms]
st = {a: stats(data[a], Path(a).name) for a in arms}
print("# Model trial\n")
print("| | " + " | ".join(names) + " |")
print("|---|" + "---|" * len(arms))
rows = [
    ("Ticks played", lambda s: str(s["ticks"])),
    ("Cost for the day", lambda s: f"${s['cost']:.3f}"),
    ("Cost per tick", lambda s: f"${s['per_tick']:.4f}"),
    ("Cost per month (15 ticks/day)", lambda s: f"${s['per_tick'] * 15 * 30:.2f}"),
    ("Replies that needed a retry (bad JSON)", lambda s: str(s["retries"])),
    ("Actions the engine had to refuse", lambda s: str(s["notes"])),
    ("Lines spoken", lambda s: str(s["lines"])),
    ("Average words per line", lambda s: f"{s['avg_words']:.1f}"),
    ("Lines repeated word for word", lambda s: str(s["repeats"])),
    ("Places visited per resident", lambda s: f"{s['zones']:.1f}"),
    ("Action mix", lambda s: ", ".join(f"{k} {v}" for k, v in s["acts"].most_common())),
]
for label, fn in rows:
    print(f"| {label} | " + " | ".join(fn(st[a]) for a in arms) + " |")
print("\n## The day, tick by tick\n")
n = max(len(v) for v in data.values())
for i in range(n):
    first = next((data[a][i] for a in arms if i < len(data[a])), None)
    print(f"### {first['simulation_time'] if first else i}\n")
    for a in arms:
        if i < len(data[a]):
            t = data[a][i]
            print(f"**{Path(a).name}:** {t['story']}\n")
            for r_id, r in t["results"].items():
                act = r.get("action", {})
                if act.get("type") == "SPEAK":
                    p = act.get("parameters", {})
                    print(f"- {r_id} to {p.get('target')}: “{p.get('content')}”")
            print()
