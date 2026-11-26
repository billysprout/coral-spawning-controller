#!/usr/bin/env python3
"""cue_import.py -- install reef cue tables from a cue-table repo into the data folder.

    python cue_import.py fetch  [--url URL] [--ref REF] [--from-dir DIR] [--data DIR] [--force]
    python cue_import.py ensure [--data DIR]       fetch only when no cue data is installed yet
    python cue_import.py status [--data DIR]

The cue tables (per-reef sky and watch tables, satellite SST, the Okinawa
reference parameters and calendar, the arc catalog and the audit manifest) live
in their own repository, by default
https://github.com/realfakedoors-idaho/reef-cue-tables. Its index.json follows
cue_schema.py (format version 1): every file with its kind, sha256 and where it
goes under data/.

fetch downloads <url>/archive/<ref>.tar.gz (or reads --from-dir), validates the
whole repo (schema version, every hash, every column list, safe install paths)
in a temporary folder, and only then copies the files in. Without --force it
refuses when an installed file differs from the repo's (a calendar or table you
changed on purpose stays yours); files that are already identical are left as
they are. It writes <data>/cue_tables.json with the source, ref and index hash.

ensure is what the containers run at start (tank_supervisor.py, start.sh): it
does nothing when <data>/cue_tables.json exists or cue data is already in place
(a calendar or locale_profiles.csv), and otherwise fetches. A failed fetch is
reported and the controller starts anyway (it then warns that it has no
calendar).

Settings (environment, .env): CUE_TABLES_URL, CUE_TABLES_REF (a tag is safest;
default main), CUE_TABLES_AUTO_IMPORT (1 = ensure runs at start; 0 = never).
Reading a public repo needs no token. Standard library only.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import shutil
import sys
import tarfile
import tempfile
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
import cue_schema  # noqa: E402

DEFAULT_URL = "https://github.com/realfakedoors-idaho/reef-cue-tables"
DEFAULT_REF = "main"
STATE = "cue_tables.json"
MAX_BYTES = 200 * 1024 * 1024


def data_dir(arg=None) -> str:
    return arg or os.environ.get("TANK_DATA") or os.path.join(os.path.dirname(_HERE), "data")


def _download(url: str, ref: str, dst: str) -> str:
    """Fetch the repo archive and unpack it safely; returns the repo root."""
    arc = f"{url.rstrip('/')}/archive/{ref}.tar.gz"
    req = urllib.request.Request(arc, headers={"User-Agent": "coral-spawning-controller cue_import"})
    tgz = os.path.join(dst, "repo.tar.gz")
    with urllib.request.urlopen(req, timeout=60) as resp, open(tgz, "wb") as fh:
        n = 0
        while True:
            b = resp.read(1 << 16)
            if not b:
                break
            n += len(b)
            if n > MAX_BYTES:
                raise SystemExit(f"cue_import: archive larger than {MAX_BYTES // 2**20} MB, refusing")
            fh.write(b)
    out = os.path.join(dst, "x")
    os.makedirs(out)
    with tarfile.open(tgz, "r:gz") as tf:
        for m in tf.getmembers():
            name = m.name.replace("\\", "/")
            if name.startswith("/") or ".." in name.split("/") or not (m.isfile() or m.isdir()):
                raise SystemExit(f"cue_import: unsafe entry in archive: {m.name}")
        tf.extractall(out, filter="data") if hasattr(tarfile, "data_filter") else tf.extractall(out)
    tops = os.listdir(out)
    if len(tops) != 1:
        raise SystemExit("cue_import: archive does not have one top folder")
    return os.path.join(out, tops[0])


def _installed_present(data: str) -> bool:
    return (os.path.exists(os.path.join(data, STATE))
            or bool(glob.glob(os.path.join(data, "spawn_calendar_*.csv")))
            or os.path.exists(os.path.join(data, "locales", "locale_profiles.csv")))


def fetch(data: str, url: str, ref: str, from_dir: str | None, force: bool) -> int:
    tmp = tempfile.mkdtemp(prefix="cue-import-")
    try:
        root = os.path.abspath(from_dir) if from_dir else _download(url, ref, tmp)
        idx, errs = cue_schema.validate(root)
        if errs:
            print("cue_import: the cue-table repo did not validate:", file=sys.stderr)
            for e in errs[:40]:
                print("  " + e, file=sys.stderr)
            return 2
        plan, conflicts = [], []
        for e in idx["files"]:
            if not e.get("install"):
                continue
            src = os.path.join(root, *e["path"].split("/"))
            dst = os.path.join(data, *e["install"][len("data/"):].split("/"))
            if os.path.exists(dst):
                if cue_schema.sha(dst)[0] == e["sha256"]:
                    continue
                if not force:
                    conflicts.append(e["install"])
                    continue
            plan.append((src, dst))
        if conflicts:
            print(f"cue_import: {len(conflicts)} installed files differ from the repo; nothing copied. "
                  "Keep yours, or run again with --force to replace them:", file=sys.stderr)
            for c in conflicts[:40]:
                print("  " + c, file=sys.stderr)
            return 3
        for src, dst in plan:
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            part = dst + ".part"
            shutil.copyfile(src, part)
            os.replace(part, dst)
        with open(os.path.join(root, "index.json"), "rb") as fh:
            ih = hashlib.sha256(fh.read()).hexdigest()
        state = {"source": from_dir and os.path.abspath(from_dir) or url, "ref": None if from_dir else ref,
                 "index_sha256": ih, "schema_version": idx.get("schema_version"),
                 "source_commit": idx.get("source_commit"), "files": len(idx["files"]),
                 "copied": len(plan), "imported_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        os.makedirs(data, exist_ok=True)
        with open(os.path.join(data, STATE), "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, indent=1)
            fh.write("\n")
        print(f"cue_import: {len(plan)} files copied, {len(idx['files']) - len(plan)} already in place "
              f"({len(idx.get('locales', []))} locales, schema {idx.get('schema_version')}) -> {data}")
        return 0
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="install reef cue tables")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "ensure", "status"):
        p = sub.add_parser(name)
        p.add_argument("--data", default=None)
        if name != "status":
            p.add_argument("--url", default=os.environ.get("CUE_TABLES_URL") or DEFAULT_URL)
            p.add_argument("--ref", default=os.environ.get("CUE_TABLES_REF") or DEFAULT_REF)
            p.add_argument("--from-dir", default=None, help="a local copy of the cue-table repo")
        if name == "fetch":
            p.add_argument("--force", action="store_true", help="replace installed files that differ")
    a = ap.parse_args(argv)
    data = data_dir(a.data)
    if a.cmd == "status":
        p = os.path.join(data, STATE)
        print(open(p, encoding="utf-8").read() if os.path.exists(p) else "no cue tables imported")
        return 0
    if a.cmd == "ensure":
        if os.environ.get("CUE_TABLES_AUTO_IMPORT", "1") == "0":
            print("cue_import: CUE_TABLES_AUTO_IMPORT=0, not importing")
            return 0
        if _installed_present(data):
            return 0
        try:
            rc = fetch(data, a.url, a.ref, a.from_dir, False)
        except (OSError, SystemExit, tarfile.TarError) as e:
            print(f"cue_import: could not fetch the cue tables from {a.url} ({a.ref}): {e}", file=sys.stderr)
            return 0
        return 0 if rc in (0, 3) else rc
    return fetch(data, a.url, a.ref, a.from_dir, a.force)


if __name__ == "__main__":
    sys.exit(main())
