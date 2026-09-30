"""Report stat definitions added/removed per resource versus git HEAD, and count
instances that still set a stat whose definition was removed.

Removing a stat definition silently drops that value from every instance that
sets it, so a removed-but-still-used stat is a real data-loss regression.
"""

import json
import os
import re
import subprocess
import sys
from collections import defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECL = re.compile(
    r"^(?:base|calc)\s+(?:bool|float|integer|string|resource<[^>]+>|resource<[^>]+>\[\]"
    r"|visual_trait|visual_trait\[\]|enum<[^>]+>|string\[\])\s+"
    r"([A-Za-z0-9_]+)\s*\(",
)
METADATA = {"id", "updated_at", "photo"}


def declared(text):
    return {m.group(1) for m in (DECL.match(l.strip()) for l in text.splitlines()) if m}


def head_stats(system, rid):
    path = "systems/%s/system/resources/%s/stats.rpgs" % (system, rid)
    try:
        out = subprocess.check_output(["git", "show", "HEAD:" + path], cwd=REPO,
                                      stderr=subprocess.DEVNULL)
    except subprocess.CalledProcessError:
        return None
    return declared(out.decode("utf-8", "replace"))


def now_stats(system, rid):
    path = os.path.join(REPO, "systems", system, "system", "resources", rid, "stats.rpgs")
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as fh:
        return declared(fh.read())


def instance_keys(system, rid):
    inst_dir = os.path.join(REPO, "systems", system, "resource_instances")
    out = defaultdict(list)
    if not os.path.isdir(inst_dir):
        return out
    for fname in sorted(os.listdir(inst_dir)):
        if not fname.endswith(".json"):
            continue
        try:
            with open(os.path.join(inst_dir, fname), encoding="utf-8") as fh:
                data = json.load(fh)
        except Exception:  # noqa: BLE001
            continue
        if data.get("resource_id") != rid:
            continue
        for key in (data.get("stats") or {}):
            if key not in METADATA:
                out[key].append(fname)
    return out


def main():
    for system in (sys.argv[1:] or ["5e"]):
        root = os.path.join(REPO, "systems", system)
        res_root = os.path.join(root, "system", "resources")
        print("\n=== %s: stats removed vs HEAD ===" % system)
        problems = 0
        for rid in sorted(os.listdir(res_root)):
            if not os.path.isdir(os.path.join(res_root, rid)):
                continue
            before = head_stats(system, rid)
            after = now_stats(system, rid)
            if before is None or after is None:
                continue
            removed = before - after
            added = after - before
            if not removed and not added:
                continue
            used = instance_keys(system, rid)
            print("\n  %s" % rid)
            if added:
                print("    added:   %s" % ", ".join(sorted(added)))
            for stat in sorted(removed):
                n = len(used.get(stat, []))
                flag = "  <-- DATA LOSS" if n else "  (unused)"
                print("    removed: %-28s %d instances still set it%s"
                      % (stat, n, flag))
                if n:
                    problems += 1
                    for f in used[stat][:3]:
                        print("               e.g. %s" % f)
        print("\n  regression count: %d" % problems)


if __name__ == "__main__":
    main()