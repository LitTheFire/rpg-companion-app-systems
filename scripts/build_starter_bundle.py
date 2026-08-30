#!/usr/bin/env python3
"""Build the bundled-starter subset of a system (PR 7).

Stages a filtered copy of one system (same `system/` sources, only the
resource instances listed in its starter manifest), runs the SAME release
builder the full pipeline uses, and validates the resulting triple
(system.rpg, resources.rpg, resources.rpg.gzip). Because the sources and
builder are identical, the subset's system.rpg carries the same version as
the full release built from this checkout — which is load-bearing for the
app's isNewer check (see docs/growth-plans/bundled-starter-system.md in the
app repo).

Usage:
  scripts/build_starter_bundle.py                       # 5e2024, default paths
  scripts/build_starter_bundle.py --install-app-assets  # also copy into the
                                                        # app's assets dir

Output: releases/starter/<system>/ {system.rpg, resources.rpg,
resources.rpg.gzip, resources.json} + a manifest echo for traceability.
Local-only: never touches S3/CloudFront.
"""

import argparse
import gzip
import io
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMS_DIR = REPO_ROOT / "systems"
DEV_TOOL_DIR = Path.home() / "Repositories" / "RPGCompanionApp" / "dev_tool"
APP_ASSETS_DIR = (Path.home() / "Repositories" / "RPGCompanionApp" / "app"
                  / "assets" / "bundled_systems")
DEFAULT_OUT = REPO_ROOT / "releases" / "starter"
BUDGET_BYTES = int(3.0 * 1024 * 1024)


class Fatal(Exception):
    pass


def log(msg=""):
    print(msg, flush=True)


def read_manifest(path):
    if not path.is_file():
        raise Fatal(f"Manifest not found: {path}")
    names = []
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        names.append(line)
    if not names:
        raise Fatal(f"Manifest {path} lists no files")
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        raise Fatal(f"Manifest has duplicate entries: {sorted(dupes)[:5]}…")
    return names


def stage(system_id, manifest_names, stage_root):
    """Build <stage_root>/systems with _stdlib + the filtered system."""
    staged_systems = stage_root / "systems"
    src_system = SYSTEMS_DIR / system_id
    if not src_system.is_dir():
        raise Fatal(f"Unknown system: {src_system}")

    # _stdlib is needed for cross-system rpgs imports; copy as-is.
    stdlib = SYSTEMS_DIR / "_stdlib"
    if stdlib.is_dir():
        shutil.copytree(stdlib, staged_systems / "_stdlib")

    dst = staged_systems / system_id
    shutil.copytree(src_system / "system", dst / "system")

    instances_src = src_system / "resource_instances"
    instances_dst = dst / "resource_instances"
    instances_dst.mkdir(parents=True)
    missing = []
    for name in manifest_names:
        f = instances_src / name
        if not f.is_file():
            missing.append(name)
            continue
        shutil.copy2(f, instances_dst / name)
    if missing:
        raise Fatal(f"{len(missing)} manifest entries have no source file, "
                    f"e.g.: {missing[:5]}")
    return staged_systems


def run_builder(staged_systems, build_out):
    if shutil.which("dart") is None:
        raise Fatal("dart not found on PATH")
    proc = subprocess.run(
        ["dart", "run", "tool/refresh_system_builder.dart",
         f"--base={staged_systems}", f"--output={build_out}", "--clean"],
        cwd=DEV_TOOL_DIR, capture_output=True, text=True)
    # Builder warnings are expected (same ones as the full build); errors are not.
    if proc.returncode != 0:
        sys.stderr.write(proc.stdout[-4000:] if proc.stdout else "")
        sys.stderr.write(proc.stderr[-2000:] if proc.stderr else "")
        raise Fatal("Subset build failed")


def gunzip_json(path):
    return json.loads(gzip.decompress(path.read_bytes()))


def validate(system_id, build_out, manifest_names):
    sys_out = build_out / system_id
    triple = ["system.rpg", "resources.rpg", "resources.rpg.gzip"]
    for name in triple + ["resources.json"]:
        f = sys_out / name
        if not f.is_file() or f.stat().st_size == 0:
            raise Fatal(f"Subset build missing {f}")

    # Version must match the source manifest (and thus the full release
    # built from this same checkout).
    src_version = json.loads(
        (SYSTEMS_DIR / system_id / "system" / "system.rpg.json")
        .read_text())["version"]
    built = gunzip_json(sys_out / "system.rpg")
    if built.get("version") != src_version:
        raise Fatal(f"Subset version {built.get('version')} != source "
                    f"{src_version}")

    # Every manifest instance must have made it into the subset index, with
    # an updated_at (the app-side install seeds the sync ledger from these).
    index = json.loads((sys_out / "resources.json").read_text())
    entries = index.get("resources", index if isinstance(index, list) else [])
    expected = len(manifest_names)
    if len(entries) != expected:
        raise Fatal(f"Subset index has {len(entries)} resources; manifest "
                    f"lists {expected}")
    without_ts = [e.get("id") for e in entries
                  if not (e.get("updated_at") or e.get("last_update"))]
    if without_ts:
        raise Fatal(f"{len(without_ts)} subset resources lack updated_at "
                    f"(ledger seeding would mis-key), e.g. {without_ts[:5]}")

    bulk = (sys_out / "resources.rpg.gzip").stat().st_size
    total = sum((sys_out / n).stat().st_size for n in triple)
    log(f"✅ subset valid: version {src_version}, {len(entries)} resources, "
        f"bulk {bulk/1024/1024:.2f} MB, triple total {total/1024/1024:.2f} MB")
    if total > BUDGET_BYTES:
        raise Fatal(f"Bundle {total} B exceeds the {BUDGET_BYTES} B budget — "
                    "trim the manifest (items first).")
    return src_version


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--system", default="5e2024")
    ap.add_argument("--manifest", type=Path, default=None,
                    help="default: systems/<system>/starter_manifest.txt")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--install-app-assets", action="store_true",
                    help=f"also copy the triple into {APP_ASSETS_DIR}/<system>/")
    args = ap.parse_args()

    manifest_path = args.manifest or (
        SYSTEMS_DIR / args.system / "starter_manifest.txt")
    try:
        names = read_manifest(manifest_path)
        log(f"📦 Starter subset for {args.system}: {len(names)} instances "
            f"from {manifest_path.name}")
        with tempfile.TemporaryDirectory(prefix="starter_build_") as tmp:
            tmp = Path(tmp)
            staged = stage(args.system, names, tmp)
            log("🚧 Building subset (same builder, same sources)…")
            run_builder(staged, tmp / "out")
            version = validate(args.system, tmp / "out", names)

            dest = args.out / args.system
            if dest.exists():
                shutil.rmtree(dest)
            dest.mkdir(parents=True)
            for name in ["system.rpg", "resources.rpg", "resources.rpg.gzip",
                         "resources.json"]:
                shutil.copy2(tmp / "out" / args.system / name, dest / name)
            shutil.copy2(manifest_path, dest / "starter_manifest.txt")
            log(f"📁 Wrote {dest} (v{version})")

            if args.install_app_assets:
                app_dest = APP_ASSETS_DIR / args.system
                if app_dest.exists():
                    shutil.rmtree(app_dest)
                app_dest.mkdir(parents=True)
                # The app bundle needs the triple + the index (ledger seeding
                # reads resources.rpg; resources.json stays out — dead weight).
                for name in ["system.rpg", "resources.rpg",
                             "resources.rpg.gzip"]:
                    shutil.copy2(dest / name, app_dest / name)
                log(f"📲 Installed app assets: {app_dest}")
        return 0
    except Fatal as e:
        print(f"\n❌ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
