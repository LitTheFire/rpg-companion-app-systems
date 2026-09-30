"""Real RPGScript build validation for the 5e / 5e2024 fusion.

Runs the actual dev-tool builder (bundled `refresh_system_builder.exe`) against a
throwaway copy of `systems/`, then asserts the compiled output is correct.

This never writes inside the repository: the systems tree is copied to a temp
scratch directory, the build output goes to a temp directory, and the repo is
only ever read.

Toolchain gap (pre-existing, NOT caused by this fusion)
-------------------------------------------------------
Extension 1.3.0 is the newest version published on the marketplace (2026-06-08).
Upstream added `is_defeated` to `combatant monster_instance` on 2026-08-24
(commit 6d590c92f), so 1.3.0 cannot type-check any system in the repo -- it
fails identically on pristine HEAD and on `pf2e`, which this fusion never
touches. The dev_tool source is private, so a newer builder cannot be built
locally.

To still get full type-checking coverage, this script strips ONLY those known
incompatible constructs from the scratch copy and reports exactly what it
stripped. Everything else is compiled and type-checked for real. The build
output is therefore a validation artifact only -- never a shippable package,
because it is missing `is_defeated`.

Usage
-----
    python scripts/validate_build.py
    python scripts/validate_build.py --keep     # keep scratch dirs for inspection
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SYSTEMS = ("5e", "5e2024")
EXPECTED_BOONS = 27
MAX_LEVEL = 50

# Shipped 5e thresholds that must survive the level-50 extension.
PRESERVED_XP = ((0, 1), (900, 3), (355000, 20))
TAIL_XP = ((385000, 21), (415000, 22), (1255000, 50))

# Construct the 1.3.0 builder cannot type-check. Keyed by repo-relative path.
KNOWN_INCOMPATIBLE = (
    (
        "system/combat_system/combatant_types/monster_instance/index.rpgs",
        re.compile(rb"^[ \t]*is_defeated[ \t]*=.*\r?\n", re.M),
        "is_defeated (added upstream 2026-08-24, after tool 1.3.0)",
    ),
)


def find_builder() -> str:
    """Locate the bundled dev-tool builder for this platform."""
    plat = {"win32": "win32-x64", "darwin": "darwin-arm64", "linux": "linux-x64"}[
        sys.platform
    ]
    roots = [
        os.path.expanduser("~/.vscode/extensions"),
        os.path.expanduser("~/.vscode-insiders/extensions"),
    ]
    for root in roots:
        if not os.path.isdir(root):
            continue
        for name in sorted(os.listdir(root), reverse=True):
            if name.lower().startswith("blastervlaenterprisesllc.rpgscript-"):
                exe = "refresh_system_builder.exe" if plat == "win32-x64" else "refresh_system_builder"
                cand = os.path.join(root, name, "bundled_tools", plat, exe)
                if os.path.isfile(cand):
                    return cand
    raise SystemExit(
        "Could not find refresh_system_builder in any VS Code extensions dir.\n"
        "Install the 'RPGScript' extension (BlastervlaEnterprisesLLC.rpgscript)."
    )


def modified_paths() -> set[str]:
    """Tracked files with local modifications, as repo-relative POSIX paths."""
    out = subprocess.run(
        ["git", "status", "--porcelain", "-uno"],
        cwd=REPO,
        capture_output=True,
        text=True,
    ).stdout
    paths = set()
    for line in out.splitlines():
        p = line[3:].strip().strip('"')
        if p and p[-4:] != ".rpg":
            paths.add(p.replace(os.sep, "/"))
    return paths


def prepare_scratch(dest: str) -> list[str]:
    """Copy systems/ to dest and strip known tool-incompatible constructs."""
    shutil.copytree(
        os.path.join(REPO, "systems"), dest, ignore=shutil.ignore_patterns("__pycache__")
    )
    stripped = []
    for system in ("5e", "5e2024", "pf2e"):
        for rel, pattern, why in KNOWN_INCOMPATIBLE:
            path = os.path.join(dest, system, rel)
            if not os.path.isfile(path):
                continue
            with open(path, "rb") as fh:
                blob = fh.read()
            new = pattern.sub(b"", blob)
            if new != blob:
                with open(path, "wb") as fh:
                    fh.write(new)
                stripped.append("%s/%s  ->  %s" % (system, rel, why))
    return stripped


def run_build(builder: str, base: str, out: str) -> tuple[dict, int]:
    cmd = [builder, "--base=%s" % base, "--output=%s" % out, "--dev",
           "--structured-diagnostics"]
    proc = subprocess.run(cmd, capture_output=True, text=True, errors="replace")
    blob = proc.stdout + proc.stderr
    i, j = blob.find("{"), blob.rfind("}")
    payload = {}
    if i != -1 and j > i:
        try:
            payload = json.loads(blob[i : j + 1])
        except json.JSONDecodeError:
            pass
    return payload, proc.returncode


def load_rpg(path: str) -> dict:
    raw = open(path, "rb").read()
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    return json.loads(raw.decode("utf-8"))


def check_system(out: str, system: str, touched: set[str], fail: list[str]) -> None:
    print("-- %s" % system)
    index = json.load(
        open(os.path.join(out, system, "resources.json"), encoding="utf-8")
    )["resources"]
    boons = [r for r in index if "Boon of" in (r.get("name") or "")]
    print("   boons in built index: %d" % len(boons))
    if len(boons) != EXPECTED_BOONS:
        fail.append("%s: expected %d boons, built %d" % (system, EXPECTED_BOONS, len(boons)))

    counts = {"type": 0, "source": 0, "prereq": 0, "level": 0}
    for b in boons:
        path = os.path.join(out, system, b["path"])
        if not os.path.isfile(path):
            fail.append("%s: %s missing from build output" % (system, b["name"]))
            continue
        st = load_rpg(path).get("stats", {})
        name = b["name"]
        if (st.get("type") or {}).get("value") == "epic_boon":
            counts["type"] += 1
        else:
            fail.append("%s: %s type=%r" % (system, name, (st.get("type") or {}).get("value")))
        if (st.get("source") or {}).get("value") == "dmg":
            counts["source"] += 1
        else:
            fail.append("%s: %s source=%r" % (system, name, (st.get("source") or {}).get("value")))
        if (st.get("prerequisites") or {}).get("value") == "Level 20+":
            counts["prereq"] += 1
        else:
            fail.append("%s: %s prerequisites=%r" % (system, name, (st.get("prerequisites") or {}).get("value")))
        level = None
        for de in (st.get("descriptions") or {}).get("value") or []:
            if isinstance(de, dict) and de.get("resource_id") == "levelled_description":
                level = (de.get("stats", {}).get("level") or {}).get("value")
        if level == 20:
            counts["level"] += 1
        else:
            fail.append("%s: %s levelled_description.level=%r" % (system, name, level))

    print("   epic_boon type      %2d/%d" % (counts["type"], EXPECTED_BOONS))
    print("   source == dmg      %2d/%d" % (counts["source"], EXPECTED_BOONS))
    print("   'Level 20+' prereq %2d/%d" % (counts["prereq"], EXPECTED_BOONS))
    print("   levelled level 20  %2d/%d" % (counts["level"], EXPECTED_BOONS))

    comp = json.load(
        open(os.path.join(out, system, "system.composed.json"), encoding="utf-8")
    )
    table = None
    for tab in comp["progression_systems"]["experience_levelling_system"]["tables"]:
        cand = tab.get("experience_to_level_table")
        if isinstance(cand, dict) and len(cand) > 20:
            table = cand
            break
    if table is None:
        fail.append("%s: experience_to_level_table not found in composed output" % system)
        return
    pairs = {
        int(k): v
        for k, v in table.items()
        if isinstance(k, str) and k.lstrip("-").isdigit() and isinstance(v, int)
    }
    levels = sorted(set(pairs.values()))
    ok_levels = levels == list(range(1, MAX_LEVEL + 1))
    print("   XP levels %d..%d complete_1_%d=%s"
          % (levels[0], levels[-1], MAX_LEVEL, ok_levels))
    if not ok_levels:
        fail.append("%s: XP levels are not exactly 1..%d (%d distinct)"
                    % (system, MAX_LEVEL, len(levels)))
    for xp, want in tuple(PRESERVED_XP) + tuple(TAIL_XP):
        got = pairs.get(xp)
        flag = "ok" if got == want else "MISMATCH"
        print("   xp %8d -> %-4s (%s)" % (xp, got, flag))
        if got != want:
            fail.append("%s: xp %d -> %s, expected %s" % (system, xp, got, want))
    return touched and None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="keep scratch dirs")
    args = ap.parse_args()

    builder = find_builder()
    print("builder : %s" % builder)
    print("repo    : %s" % REPO)

    root = tempfile.mkdtemp(prefix="rpg-validate-")
    base = os.path.join(root, "systems")
    out = os.path.join(root, "out")
    fail: list[str] = []

    try:
        stripped = prepare_scratch(base)
        print("\nstripped from scratch copy (tool 1.3.0 incompatibility):")
        for s in stripped or ["   (nothing)"]:
            print("   %s" % s)
        if not stripped:
            print("   (none -- 1.3.0 may now accept upstream constructs)")

        payload, code = run_build(builder, base, out)
        counts = payload.get("counts", {})
        print("\nbuild exit=%d  diagnostics=%s errors=%s warnings=%s"
              % (code, counts.get("diagnostics"), counts.get("errors"), counts.get("warnings")))

        diags = payload.get("diagnostics", [])
        errs = [d for d in diags if d.get("severity") == "error"]
        if errs:
            fail.append("%d build error(s); first: %s"
                        % (len(errs), errs[0].get("message", "")[:160]))
        for e in errs[:10]:
            print("   ERROR %s:%s %s" % (e.get("file_path"), e.get("line"),
                                         e.get("message", "")[:150]))

        touched = modified_paths()
        mine = [d for d in diags
                if d.get("severity") in ("error", "warning")
                and any(t in d.get("file_path", "").replace(os.sep, "/").replace("\\", "/")
                        for t in touched)]
        print("   diagnostics in files you modified: %d (of %d touched)"
              % (len(mine), len(touched)))
        for m in mine[:10]:
            print("   %s %s:%s" % (m.get("severity", "").upper(),
                                  m.get("file_path"), m.get("line")))

        for system in SYSTEMS:
            check_system(out, system, touched, fail)
    finally:
        if args.keep:
            print("\nkept: %s" % root)
        else:
            shutil.rmtree(root, ignore_errors=True)

    print("\n" + "=" * 60)
    if fail:
        print("BUILD VALIDATION FAILED (%d)" % len(fail))
        for f in fail:
            print("  - %s" % f)
        return 1
    print("BUILD VALIDATION PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
