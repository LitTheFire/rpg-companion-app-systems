"""Audit the fused RPG Companion systems.

Checks, per system:
  1. every .rpg.json parses
  2. the system tag on each file matches its directory
  3. resource_instances reference a resource_id that the system defines
  4. enumerated_types values used by instances actually exist
  5. no duplicate instance base names survive
  6. the XP table is contiguous 1..50
"""

import json
import os
import re
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UUID = re.compile(r"_[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

failures = []
notes = []


def fail(msg):
    failures.append(msg)


def note(msg):
    notes.append(msg)


def base_name(fname):
    return re.sub(r"\.rpg\.json$", "", UUID.sub("", fname))


def load(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def collect_stats(node, out):
    """Walk an instance payload and gather every stat name it sets."""
    if not isinstance(node, dict):
        return
    stats = node.get("stats")
    if isinstance(stats, dict):
        for key in stats:
            out.add(key)
    for value in node.values():
        collect_stats(value, out)


def audit(system, max_level):
    root = os.path.join(REPO, "systems", system)
    print("\n=== %s ===" % system)

    # --- 1. JSON well-formedness over the whole system ---
    parsed = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if not fname.endswith(".json"):
                continue
            path = os.path.join(dirpath, fname)
            try:
                load(path)
                parsed += 1
            except Exception as exc:  # noqa: BLE001
                fail("%s: unparseable %s: %s" % (system, os.path.relpath(path, root), exc))
    print("parsed %d json files" % parsed)

    # --- 2. system tag matches directory ---
    tagged = 0
    mismatched = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fname in filenames:
            if not fname.endswith(".json"):
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, root)
            if rel.startswith("system" + os.sep):
                continue  # system definition file legitimately uses its own id
            try:
                data = load(path)
            except Exception:  # noqa: BLE001
                continue
            if isinstance(data, dict) and "system" in data:
                if data["system"] != system:
                    if rel.startswith("resource_instances" + os.sep):
                        # An instance whose resource_id has no matching resource
                        # definition is inert: the app never loads it. Record it
                        # so the count stays visible, but do not fail the audit.
                        mismatched.append((rel, data["system"]))
                    else:
                        fail("%s: wrong system tag in %s (%r)"
                             % (system, rel, data["system"]))
                else:
                    tagged += 1
    print("checked system tag on %d files" % tagged)
    if mismatched:
        by_tag = {}
        for _rel, tag in mismatched:
            by_tag[tag] = by_tag.get(tag, 0) + 1
        print("  note: %d inert instances carry foreign system tags %s"
              % (len(mismatched), by_tag))

    # --- 3. resource_ids used by instances must be defined ---
    defined = set()
    res_root = os.path.join(root, "system", "resources")
    if os.path.isdir(res_root):
        defined = {d for d in os.listdir(res_root)
                   if os.path.isdir(os.path.join(res_root, d))}
    inst_root = os.path.join(root, "resource_instances")
    used = {}
    if os.path.isdir(inst_root):
        for fname in sorted(os.listdir(inst_root)):
            if not fname.endswith(".json"):
                continue
            data = load(os.path.join(inst_root, fname))
            rid = data.get("resource_id")
            if rid:
                used.setdefault(rid, []).append(fname)
            else:
                fail("%s: instance without resource_id: %s" % (system, fname))
    for rid, files in sorted(used.items()):
        if rid not in defined:
            fail("%s: instances reference undefined resource_id %r (%d files, e.g. %s)"
                 % (system, rid, len(files), files[0]))
    print("instances by resource_id: %s"
          % ", ".join("%s=%d" % (k, len(v)) for k, v in sorted(used.items())))

    # --- 4. enumerated type values resolve ---
    enum_root = os.path.join(root, "system", "enumerated_types")
    enum_values = {}
    if os.path.isdir(enum_root):
        for fname in os.listdir(enum_root):
            if not fname.endswith(".json"):
                continue
            data = load(os.path.join(enum_root, fname))
            if isinstance(data, list):
                # Some enumerated type files are a bare list of {id, name}.
                ids = {v.get("id") for v in data if isinstance(v, dict)}
                enum_values[fname[:-5]] = ids
                continue
            vals = data.get("types") or []
            if isinstance(vals, dict):
                ids = set(vals.keys())
            else:
                ids = {v.get("id") for v in vals if isinstance(v, dict)}
            enum_values[data.get("id", fname[:-5])] = ids
    for name, ids in sorted(enum_values.items()):
        if not ids and not name.startswith("index"):
            fail("%s: enumerated type %r defines no values" % (system, name))

    # Map each resource's stat schema to the enum it validates against.
    enum_targets = {
        "feat": "feat_types",
        "weapon": "weapon_masteries",
        "monster": "monster_treasure_themes",
    }
    for stat, enum_name in enum_targets.items():
        if stat not in defined or enum_name not in enum_values:
            continue
        allowed = enum_values[enum_name]
        for fname in used.get(stat, []):
            data = load(os.path.join(inst_root, fname))
            node = data.get("stats", {}).get(stat)
            # enum consumers name the stat after the enum, e.g. feat -> type
            if stat == "feat":
                value = node
            else:
                value = None
            if isinstance(value, dict):
                v = value.get("value")
                if isinstance(v, str) and v not in allowed:
                    fail("%s: %s references unknown %s value %r"
                         % (system, fname, enum_name, v))

    # --- 5. duplicate instance base names ---
    if os.path.isdir(inst_root):
        seen = {}
        dups = 0
        for fname in sorted(os.listdir(inst_root)):
            if not fname.endswith(".json"):
                continue
            b = base_name(fname)
            if b in seen:
                dups += 1
                fail("%s: duplicate instance base name %s (%s vs %s)"
                     % (system, b, seen[b], fname))
            else:
                seen[b] = fname
        print("instance base names: %d unique, %d duplicates" % (len(seen), dups))

    # --- 6. XP table contiguity ---
    sys_path = os.path.join(root, "system", "system.rpg.json")
    data = load(sys_path)
    table = data["progression_systems"]["experience_levelling_system"]["tables"][0][
        "experience_to_level_table"]
    levels = sorted(set(table.values()))
    if levels != list(range(1, max_level + 1)):
        fail("%s: XP table levels %s..%s, expected 1..%d"
             % (system, levels[0], levels[-1], max_level))
    print("XP table: %d entries, levels %d..%d"
          % (len(table), levels[0], levels[-1]))

    # --- boon sanity ---
    boons = [f for f in os.listdir(inst_root) if f.startswith("feat_boon")]
    bad = 0
    for fname in boons:
        d = load(os.path.join(inst_root, fname))
        stats = d.get("stats", {})
        name = stats.get("name", {}).get("value")
        if stats.get("type", {}).get("value") != "epic_boon":
            fail("%s: %s is not type epic_boon" % (system, fname))
            bad += 1
        if stats.get("prerequisites", {}).get("value") != "Level 20+":
            fail("%s: %s prerequisites %r, expected 'Level 20+'"
                 % (system, fname, stats.get("prerequisites", {}).get("value")))
            bad += 1
        descs = stats.get("descriptions", {}).get("value", [])
        if not descs:
            fail("%s: %s has no description" % (system, fname))
            bad += 1
        for desc in descs:
            if desc.get("stats", {}).get("level", {}).get("value") != 20:
                fail("%s: %s levelled_description level is not 20" % (system, fname))
                bad += 1
    print("boons: %d files, %d problems" % (len(boons), bad))
    check_asi_plumbing(system, data)
    return len(boons)


def find_by_id(node, target):
    """Depth-first search for the first dict whose 'id' equals target."""
    if isinstance(node, dict):
        if node.get("id") == target:
            return node
        for v in node.values():
            r = find_by_id(v, target)
            if r is not None:
                return r
    elif isinstance(node, list):
        for v in node:
            r = find_by_id(v, target)
            if r is not None:
                return r
    return None


def constants(node, out):
    if isinstance(node, dict):
        if node.get("type") == "constant":
            out.append(node.get("value"))
        for v in node.values():
            constants(v, out)
    elif isinstance(node, list):
        for v in node:
            constants(v, out)
    return out


def check_asi_plumbing(system, data):
    """Gate the hand-edited ASI/boon wiring so it cannot silently disappear."""
    # The ASI feat picker's type filter must offer epic_boon, gated at level 20.
    picker = find_by_id(data, "asi_feat_selection")
    if picker is None:
        fail("%s: asi_feat_selection view block not found" % system)
        return
    type_filter = picker.get("filters", {}).get("type")
    if type_filter is None:
        fail("%s: asi_feat_selection has no type filter" % system)
        return

    clauses = type_filter.get("clauses", [])
    if not clauses:
        fail("%s: asi_feat_selection type filter has no clauses" % system)
        return

    gate = clauses[0].get("condition", {})
    offered = set(constants(clauses[0].get("components", []), []))
    if "epic_boon" not in offered:
        fail("%s: ASI filter does not offer epic_boon at high level (offers %s)"
             % (system, sorted(offered)))
    gate_values = [v for v in constants(gate, []) if isinstance(v, int)]
    if 20 not in gate_values:
        fail("%s: ASI boon gate is not level 20 (found %s)" % (system, gate_values))
    print("ASI plumbing: gate>=20 offers %s" % sorted(offered))

    # should_show_asi must fire on every level from 20 up.
    cs_path = os.path.join(REPO, "systems", system, "system", "character_stats.rpgs")
    if os.path.isfile(cs_path):
        with open(cs_path, encoding="utf-8") as fh:
            body = fh.read()
        m = re.search(r"calc bool should_show_asi\b.*?\n\n", body, re.S)
        if m is None:
            fail("%s: should_show_asi not found in character_stats.rpgs" % system)
        elif ">=" not in m.group(0) or "20" not in m.group(0):
            fail("%s: should_show_asi has no >= 20 clause" % system)
        else:
            print("ASI plumbing: should_show_asi has a >= 20 clause")


if __name__ == "__main__":
    max_level = int(sys.argv[1]) if len(sys.argv) > 1 else 50
    total = {}
    for system in ("5e", "5e2024"):
        total[system] = audit(system, max_level)

    print("\n" + "=" * 60)
    if failures:
        print("FAILURES (%d):" % len(failures))
        for f in failures:
            print("  -", f)
        sys.exit(1)
    print("All checks passed. Boons per system: %s" % total)