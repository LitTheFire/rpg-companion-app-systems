#!/usr/bin/env python3
"""Build and publish system updates to the RPG Companion content bucket.

Automates the manual release flow:

  1. Release-build all systems (dev_tool's refresh_system_builder, isDev=false).
  2. Validate the build output (file set, gzip integrity, no "dev" versions).
  3. Diff against what is live on the CDN and print a release plan.
  4. Upload per-system files FIRST (system.rpg, resources.rpg.gzip,
     resources.rpg, resources.json), invalidate their CDN paths, and only
     THEN upload the root manifests (systems.json, systems.rpg) + invalidate.
     This ordering means clients never see a new version pointer before the
     files behind it are in place.

SAFE BY DEFAULT: without --execute nothing is written to S3/CloudFront —
the script prints the exact commands it would run. Reads (CDN fetch,
head-object, list-distributions) do happen so the plan is real.

Guards:
  * min_app_version raised  -> loud warning + typed confirmation. An app
    below min_app_version silently SKIPS the update (system_fetcher.dart:160)
    — fresh installs on that app version get NO content for the system. Only
    bump once the required app version is at 100% rollout on BOTH stores.
  * version not bumped      -> system is skipped (clients only re-download on
    a version increase, so uploading same-version content is invisible at
    best and mixed-state at worst). Override with --include-unchanged.
  * version downgrade       -> refused unless --allow-downgrade.
  * "dev" version in output -> refused (accidental dev build).
  * system live but missing from the build -> its live manifest entry is
    preserved (never silently delisted); --allow-delist to actually drop it.
  * partial publish (--only) -> the root manifest is MERGED so unpublished
    systems keep their live entry instead of pointing at files that were
    never uploaded.

Typical use:
  scripts/publish_release.py               # build + validate + dry-run plan
  scripts/publish_release.py --execute     # the real thing
  scripts/publish_release.py --only 5e,5e2024 --execute
  scripts/publish_release.py --skip-build --execute   # reuse existing staging
"""

import argparse
import collections
import datetime
import gzip
import io
import json
import shutil
import subprocess
import sys
import tarfile
import time
import urllib.request
from pathlib import Path

# --- Configuration -----------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
SYSTEMS_DIR = REPO_ROOT / "systems"
DEFAULT_STAGING = REPO_ROOT / "releases" / "staging"
# NOTE: the authoritative dev_tool lives inside the app repo (its schema tracks
# the app's engine — e.g. is_defeated landed there, not in the standalone
# rpg-companion-app-dev-tool repo, which is stale).
DEV_TOOL_DIR = Path.home() / "Repositories" / "RPGCompanionApp" / "dev_tool"
BUILDER_ENTRY = "tool/refresh_system_builder.dart"

BUCKET = "rpg-companion-system-bucket"
DEFAULT_PROFILE = "blastervla-blastervlaEnterprises"
CDN_DOMAIN = "d2dmd8x0lyn9l8.cloudfront.net"

PER_SYSTEM_FILES = ["system.rpg", "resources.rpg.gzip", "resources.rpg", "resources.json"]
ROOT_FILES = ["systems.json", "systems.rpg"]

CONTENT_TYPE_DEFAULTS = {
    ".json": "application/json",
    ".gzip": "application/x-gzip",
    ".rpg": "application/octet-stream",
}

# --- Small helpers -----------------------------------------------------------

class Fatal(Exception):
    pass


def log(msg=""):
    print(msg, flush=True)


def warn(msg):
    print(f"\033[33m⚠ {msg}\033[0m", flush=True)


def big_warn(lines):
    bar = "!" * 74
    print(f"\033[31;1m{bar}", flush=True)
    for line in lines:
        print(f"!! {line}", flush=True)
    print(f"{bar}\033[0m", flush=True)


def compare_versions(a, b):
    """Same semantics as the app's _compareVersions (system_fetcher.dart)."""
    pa = [int(x) if x.isdigit() else 0 for x in str(a).split(".")]
    pb = [int(x) if x.isdigit() else 0 for x in str(b).split(".")]
    n = max(len(pa), len(pb))
    for i in range(n):
        ai = pa[i] if i < len(pa) else 0
        bi = pb[i] if i < len(pb) else 0
        if ai != bi:
            return (ai > bi) - (ai < bi)
    return 0


def run(cmd, cwd=None, capture=False, check=True):
    if capture:
        proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    else:
        proc = subprocess.run(cmd, cwd=cwd)
    if check and proc.returncode != 0:
        detail = (proc.stderr or "").strip() if capture else ""
        raise Fatal(f"Command failed ({proc.returncode}): {' '.join(cmd)}\n{detail}")
    return proc


def aws(args_list, profile, capture=True, check=True):
    return run(["aws", "--profile", profile, "--no-cli-pager", *args_list],
               capture=capture, check=check)


def gunzip_json(data):
    return json.loads(gzip.decompress(data))


def gzip_bytes(data):
    # mtime=0 keeps the output deterministic run-to-run.
    buf = io.BytesIO()
    with gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as f:
        f.write(data)
    return buf.getvalue()


def fetch_url(url, timeout=30):
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# --- Steps -------------------------------------------------------------------

def build_release(staging):
    if not DEV_TOOL_DIR.is_dir():
        raise Fatal(f"dev_tool not found at {DEV_TOOL_DIR}")
    log(f"🚧 Release build: {SYSTEMS_DIR} -> {staging}")
    proc = run(
        ["dart", "run", BUILDER_ENTRY,
         f"--base={SYSTEMS_DIR}", f"--output={staging}", "--clean"],
        cwd=DEV_TOOL_DIR, capture=True, check=False)
    sys.stdout.write(proc.stdout or "")
    sys.stderr.write(proc.stderr or "")
    if proc.returncode != 0:
        raise Fatal(
            "Build failed. If this is a dependency problem, run "
            f"`dart pub get` in {DEV_TOOL_DIR} and retry.")
    if "[builder][ERROR]" in (proc.stdout or ""):
        raise Fatal("Build reported [builder][ERROR] lines — fix before publishing.")


def load_staged(staging):
    manifest_path = staging / "systems.json"
    if not manifest_path.is_file():
        raise Fatal(f"No {manifest_path}. Run without --skip-build, or point "
                    "--staging at a completed release build.")
    manifest = json.loads(manifest_path.read_text())
    systems = {s["id"]: s for s in manifest["systems"]}

    problems = []
    for sys_id, entry in systems.items():
        sys_dir = staging / entry["path"]
        if str(entry.get("version")) == "dev":
            problems.append(f"{sys_id}: version is 'dev' — this is a dev build, "
                            "not a release build")
        for name in PER_SYSTEM_FILES:
            f = sys_dir / name
            if not f.is_file() or f.stat().st_size == 0:
                problems.append(f"{sys_id}: missing/empty {f}")
        # Integrity: the .rpg files are gzip'd JSON the app gunzips.
        for name, key in (("system.rpg", None), ("resources.rpg", "resources")):
            f = sys_dir / name
            if f.is_file():
                try:
                    data = gunzip_json(f.read_bytes())
                    if key and key not in data:
                        problems.append(f"{sys_id}: {name} has no '{key}' key")
                except Exception as e:
                    problems.append(f"{sys_id}: {name} is not valid gzip'd JSON ({e})")
        f = sys_dir / "resources.json"
        if f.is_file():
            try:
                json.loads(f.read_text())
            except Exception as e:
                problems.append(f"{sys_id}: resources.json does not parse ({e})")
        f = sys_dir / "resources.rpg.gzip"
        if f.is_file() and f.read_bytes()[:2] != b"\x1f\x8b":
            problems.append(f"{sys_id}: resources.rpg.gzip is not gzip data")

        # Index/archive parity: every indexed path must be ASCII (the app's
        # tar reader decodes member names as Latin-1 — non-ASCII paths never
        # match and the resource silently never installs), unique (a colliding
        # path means one archive member serving several records), and actually
        # present in the archive.
        idx_file = sys_dir / "resources.json"
        if idx_file.is_file() and f.is_file():
            try:
                entries = json.loads(idx_file.read_text())["resources"]
                paths = [e["path"] for e in entries]
                for p, n in collections.Counter(paths).items():
                    if n > 1:
                        problems.append(f"{sys_id}: {n} index entries share "
                                        f"path {p}")
                for p in paths:
                    if any(ord(c) > 127 for c in p):
                        problems.append(f"{sys_id}: non-ASCII index path {p}")
                with tarfile.open(fileobj=io.BytesIO(
                        gzip.decompress(f.read_bytes()))) as tar:
                    members = set(tar.getnames())
                missing = [p for p in paths if p not in members]
                if missing:
                    problems.append(f"{sys_id}: {len(missing)} index paths "
                                    f"missing from the archive, e.g. "
                                    f"{missing[:3]}")
            except Exception as e:
                problems.append(f"{sys_id}: parity check failed ({e})")
    root_rpg = staging / "systems.rpg"
    if not root_rpg.is_file():
        problems.append(f"missing {root_rpg}")
    if problems:
        raise Fatal("Build output failed validation:\n  - " + "\n  - ".join(problems))
    return manifest, systems


def fetch_live_manifest():
    url = f"https://{CDN_DOMAIN}/systems.json"
    try:
        live = json.loads(fetch_url(url).decode("utf-8"))
    except Exception:
        # Fall back to the gzip'd manifest the app itself reads.
        live = gunzip_json(fetch_url(f"https://{CDN_DOMAIN}/systems.rpg"))
    return {s["id"]: s for s in live["systems"]}


def make_plan(staged_systems, live_systems, only, include_unchanged,
              allow_downgrade, allow_delist):
    publish, skipped, min_app_bumps = [], [], []

    for sys_id, entry in staged_systems.items():
        if only and sys_id not in only:
            skipped.append((sys_id, "not in --only"))
            continue
        live = live_systems.get(sys_id)
        new_v = str(entry["version"])
        if live is None:
            publish.append(sys_id)
            log(f"  🆕 {sys_id}: NEW system at {new_v}")
            continue
        old_v = str(live["version"])
        cmp = compare_versions(new_v, old_v)
        if cmp < 0 and not allow_downgrade:
            raise Fatal(f"{sys_id}: staged version {new_v} is OLDER than live "
                        f"{old_v}. Use --allow-downgrade if intentional.")
        if cmp == 0 and not include_unchanged:
            skipped.append((sys_id, f"version unchanged ({old_v}) — bump "
                                    f"systems/{sys_id}/system/system.rpg.json "
                                    "or clients will never re-download"))
            continue
        publish.append(sys_id)

        old_min = live.get("min_app_version")
        new_min = entry.get("min_app_version")
        if new_min and (old_min is None or compare_versions(new_min, old_min) > 0):
            min_app_bumps.append((sys_id, old_min, new_min))

    for sys_id, reason in skipped:
        warn(f"skipping {sys_id}: {reason}")

    missing = [sid for sid in live_systems if sid not in staged_systems]
    if missing and not allow_delist:
        warn(f"live systems missing from this build (entry preserved, files "
             f"untouched): {', '.join(missing)} — use --allow-delist to drop them")

    return publish, skipped, min_app_bumps, missing


def merged_root_manifest(staged_manifest, staged_systems, live_systems,
                         publish, allow_delist):
    """Root manifest = staged entries for published systems, live entries for
    everything else, so the pointer never gets ahead of the uploaded files."""
    entries = []
    for entry in staged_manifest["systems"]:
        sys_id = entry["id"]
        if sys_id in publish:
            entries.append(entry)
        elif sys_id in live_systems:
            entries.append(live_systems[sys_id])
        else:
            entries.append(entry)  # new system not selected: harmless either way
    if not allow_delist:
        staged_ids = {e["id"] for e in entries}
        for sys_id, live_entry in live_systems.items():
            if sys_id not in staged_ids:
                entries.append(live_entry)
    return {"systems": entries}


def s3_content_type(key, profile):
    """Preserve the content-type an existing object already has (the app never
    reads it, but CloudFront compression behavior keys off it — don't churn)."""
    proc = aws(["s3api", "head-object", "--bucket", BUCKET, "--key", key,
                "--output", "json"], profile, check=False)
    if proc.returncode == 0:
        try:
            return json.loads(proc.stdout).get("ContentType")
        except Exception:
            pass
    return CONTENT_TYPE_DEFAULTS.get(Path(key).suffix)


def upload_file(local, key, profile, execute):
    ct = s3_content_type(key, profile) if execute else None
    cmd = ["aws", "--profile", profile, "s3", "cp", str(local), f"s3://{BUCKET}/{key}"]
    if ct:
        cmd += ["--content-type", ct]
    if not execute:
        log(f"    [dry-run] {' '.join(cmd)}")
        return
    log(f"    ⬆ {key}")
    run(cmd, capture=True)
    head = json.loads(aws(["s3api", "head-object", "--bucket", BUCKET,
                           "--key", key, "--output", "json"], profile).stdout)
    expected = Path(local).stat().st_size
    if head.get("ContentLength") != expected:
        raise Fatal(f"Upload size mismatch for {key}: local {expected} vs "
                    f"S3 {head.get('ContentLength')}. Stopping before the root "
                    "manifest is touched — re-run to retry.")


def resolve_distribution_id(profile):
    proc = aws(["cloudfront", "list-distributions", "--output", "json"], profile)
    data = json.loads(proc.stdout)
    for item in data.get("DistributionList", {}).get("Items", []):
        if item.get("DomainName") == CDN_DOMAIN:
            return item["Id"]
        origins = item.get("Origins", {}).get("Items", [])
        if any(BUCKET in (o.get("DomainName") or "") for o in origins):
            return item["Id"]
    raise Fatal(f"Could not find the CloudFront distribution for {CDN_DOMAIN} / "
                f"bucket {BUCKET} in this account/profile.")


def invalidate(dist_id, paths, profile, execute, wait=True):
    if not execute:
        log(f"    [dry-run] aws cloudfront create-invalidation --distribution-id "
            f"{dist_id or '<resolved-at-runtime>'} --paths {' '.join(paths)}")
        return
    log(f"    ♻ invalidating: {' '.join(paths)}")
    proc = aws(["cloudfront", "create-invalidation", "--distribution-id", dist_id,
                "--paths", *paths, "--output", "json"], profile)
    inv_id = json.loads(proc.stdout)["Invalidation"]["Id"]
    if wait:
        log(f"    … waiting for invalidation {inv_id} to complete")
        aws(["cloudfront", "wait", "invalidation-completed",
             "--distribution-id", dist_id, "--id", inv_id], profile)
        log("    ✓ invalidation complete")


def verify_live(publish, staged_systems, timeout_s=180):
    log("🔎 Verifying the CDN now serves the new manifest…")
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            live = fetch_live_manifest()
            stale = [sid for sid in publish
                     if compare_versions(str(live.get(sid, {}).get("version", "0")),
                                         str(staged_systems[sid]["version"])) < 0]
            if not stale:
                log("✅ CDN serves the new versions.")
                return True
        except Exception as e:
            warn(f"CDN check failed ({e}); retrying")
        time.sleep(10)
    warn("CDN still serving old manifest after timeout — edge caches can lag a "
         "little; re-check manually: curl -s https://" + CDN_DOMAIN + "/systems.json")
    return False


# --- Main --------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--execute", action="store_true",
                    help="actually upload + invalidate (default: dry-run)")
    ap.add_argument("--skip-build", action="store_true",
                    help="reuse the existing staging dir instead of rebuilding")
    ap.add_argument("--staging", type=Path, default=DEFAULT_STAGING,
                    help=f"build output dir (default {DEFAULT_STAGING})")
    ap.add_argument("--only", type=str, default=None,
                    help="comma-separated system ids to publish (root manifest "
                         "is merged for the rest)")
    ap.add_argument("--profile", default=DEFAULT_PROFILE,
                    help=f"AWS profile (default {DEFAULT_PROFILE})")
    ap.add_argument("--include-unchanged", action="store_true",
                    help="publish systems whose version did not change")
    ap.add_argument("--allow-downgrade", action="store_true")
    ap.add_argument("--allow-delist", action="store_true",
                    help="allow dropping live systems missing from the build")
    ap.add_argument("--ack-min-app-version", action="store_true",
                    help="skip the interactive min_app_version confirmation "
                         "(you have verified rollout is at 100%%)")
    ap.add_argument("--no-wait", action="store_true",
                    help="do not wait for CloudFront invalidations to complete")
    args = ap.parse_args()
    only = set(args.only.split(",")) if args.only else None

    try:
        # 0. Preconditions
        if shutil.which("aws") is None:
            raise Fatal("aws CLI not found on PATH")
        if not args.skip_build and shutil.which("dart") is None:
            raise Fatal("dart not found on PATH (needed for the release build)")
        if args.execute:
            proc = aws(["sts", "get-caller-identity"], args.profile, check=False)
            if proc.returncode != 0:
                raise Fatal("AWS auth failed — run: aws sso login --profile "
                            + args.profile)

        # 1. Build
        dirty = run(["git", "status", "--porcelain", "--", "systems"],
                    cwd=REPO_ROOT, capture=True, check=False)
        if (dirty.stdout or "").strip():
            n = len(dirty.stdout.strip().splitlines())
            warn(f"systems/ has {n} uncommitted change(s) — this release will "
                 "include them and won't be reproducible from git history. "
                 "Consider committing first.")
        if not args.skip_build:
            build_release(args.staging)
        else:
            log(f"⏭ --skip-build: using existing {args.staging}")

        # 2. Validate output
        staged_manifest, staged_systems = load_staged(args.staging)
        log("📦 Staged build:")
        for sid, e in staged_systems.items():
            log(f"    {sid}: v{e['version']} (min_app {e.get('min_app_version')})")

        # 3. Live state + plan
        live_systems = fetch_live_manifest()
        log("🌐 Live on CDN:")
        for sid, e in live_systems.items():
            log(f"    {sid}: v{e['version']} (min_app {e.get('min_app_version')})")
        publish, _skipped, min_app_bumps, _missing = make_plan(
            staged_systems, live_systems, only, args.include_unchanged,
            args.allow_downgrade, args.allow_delist)
        if not publish:
            log("Nothing to publish (no version changes). Bump the version in "
                "systems/<id>/system/system.rpg.json first.")
            return 0

        log("\n📋 Release plan (in order):")
        for sid in publish:
            old = live_systems.get(sid, {}).get("version", "—")
            log(f"    {sid}: {old} -> {staged_systems[sid]['version']} "
                f"[{', '.join(PER_SYSTEM_FILES)}]")
        log("    then root: systems.json + systems.rpg, then CDN invalidation\n")

        # 4. min_app_version guard
        if min_app_bumps:
            big_warn(
                [f"min_app_version RAISED for {sid}: {old or '(none)'} -> {new}"
                 for sid, old, new in min_app_bumps] +
                ["",
                 "Apps BELOW the new min_app_version silently stop receiving this",
                 "system (fresh installs on old app builds get NO content for it).",
                 "Only proceed if the required app version is at 100% rollout on",
                 "BOTH the Play Store and the App Store."])
            if args.execute and not args.ack_min_app_version:
                answer = input("Type 'rollout-verified' to continue: ").strip()
                if answer != "rollout-verified":
                    raise Fatal("Aborted at min_app_version confirmation.")

        # 5. Root manifest (merged if partial publish)
        merged = merged_root_manifest(staged_manifest, staged_systems,
                                      live_systems, set(publish),
                                      args.allow_delist)
        staged_root = {"systems": staged_manifest["systems"]}
        if merged != staged_root:
            warn("partial publish: writing a MERGED root manifest (unpublished "
                 "systems keep their live entries)")
        root_json = json.dumps(merged, indent=2).encode("utf-8")
        root_dir = args.staging / "_publish_root"
        root_dir.mkdir(parents=True, exist_ok=True)
        (root_dir / "systems.json").write_bytes(root_json)
        (root_dir / "systems.rpg").write_bytes(gzip_bytes(root_json))

        # 6. Execute (or print) the plan
        dist_id = resolve_distribution_id(args.profile) if args.execute else None
        for sid in publish:
            sys_path = staged_systems[sid]["path"]
            log(f"▶ {sid}")
            for name in PER_SYSTEM_FILES:
                upload_file(args.staging / sys_path / name, f"{sys_path}/{name}",
                            args.profile, args.execute)
        sys_paths = sorted({staged_systems[sid]["path"] for sid in publish})
        invalidate(dist_id,
                   [f"/{p}/{name}" for p in sys_paths for name in PER_SYSTEM_FILES],
                   args.profile, args.execute, wait=not args.no_wait)

        log("▶ root manifests")
        for name in ROOT_FILES:
            upload_file(root_dir / name, name, args.profile, args.execute)
        invalidate(dist_id, ["/systems.json", "/systems.rpg"],
                   args.profile, args.execute, wait=not args.no_wait)

        # 7. Verify
        if args.execute:
            verify_live(publish, staged_systems)
            log("\n🎉 Published: " +
                ", ".join(f"{sid} v{staged_systems[sid]['version']}" for sid in publish))
            stamp = datetime.date.today().isoformat()
            log(f"   (build kept in {args.staging}; archive it if you want: "
                f"cp -r {args.staging} {REPO_ROOT / 'releases'}/{stamp})")
        else:
            log("\n🧪 DRY RUN complete — nothing was written. "
                "Re-run with --execute to publish.")
        return 0
    except Fatal as e:
        print(f"\n\033[31;1m❌ {e}\033[0m", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
