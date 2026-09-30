"""Detect instances whose stat keys no longer match their resource's declared stats.

A prior fusion pass replaced several 5e resource stat schemas with the 5e2024
(2024) equivalents. Where an instance was kept in its 2014 shape, its keys no
longer line up with the schema. This reports, per resource, which instance stat
keys are absent from the resource's declared stats.
"""

import json
import os
import re
import sys
from collections import Counter, defaultdict

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

METADATA = {"id", "updated_at", "photo"}

DECL = re.compile(
    r"^(?:base|calc)\s+(?:bool|float|integer|string|resource<[^>]+>|resource<[^>]+>\[\]"
    r"|visual_trait|visual_trait\[\]|enum<[^>]+>|string\[\])\s+"
    r"([A-Za-z0-9_]+)\s*\(",
)


def declared_stats(res_dir):
    names = set()
    stats_file = os.path.join(res_dir, "stats.rpgs")
    if not os.path.isfile(stats_file):
        return names
    with open(stats_file, encoding="utf-8") as fh:
        for line in fh:
            m = DECL.match(line.strip())
            if m:
                names.add(m.group(1))
    return names


def instance_stats(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:  # noqa: BLE001
        return None, None
    return data.get("resource_id"), (data.get("stats") or {})


def main():
    systems = sys.argv[1:] or ["5e", "5e2024"]
    for system in systems:
        root = os.path.join(REPO, "systems", system)
        inst_dir = os.path.join(root, "resource_instances")
        if not os.path.isdir(inst_dir):
            continue

        by_resource = defaultdict(list)
        for fname in sorted(os.listdir(inst_dir)):
            if not fname.endswith(".json"):
                continue
            rid, stats = instance_stats(os.path.join(inst_dir, fname))
            if rid and stats is not None:
                by_resource[rid].append((fname, set(stats.keys()) - METADATA))

        print("\n=== %s ===" % system)
        for rid, files in sorted(by_resource.items()):
            res_dir = os.path.join(root, "system", "resources", rid)
            declared = declared_stats(res_dir)
            if not declared:
                print("  %-18s no stats.rpgs (%d instances)" % (rid, len(files)))
                continue

            unknown = Counter()
            files_with_unknown = defaultdict(list)
            for fname, keys in files:
                extra = keys - declared
                if extra:
                    for k in extra:
                        unknown[k] += 1
                    files_with_unknown[frozenset(extra)].append(fname)

            total = len(files)
            clean = total - len({f for fs in files_with_unknown for f in files_with_unknown[fs]})
            if not unknown:
                print("  %-18s %5d instances, all keys declared" % (rid, total))
                continue

            affected = sum(len(v) for v in files_with_unknown.values())
            print("  %-18s %5d instances, %d clean, %d with undeclared keys"
                  % (rid, total, clean, affected))
            for key, count in unknown.most_common(8):
                print("        %-28s %5d instances" % (key, count))


if __name__ == "__main__":
    main()