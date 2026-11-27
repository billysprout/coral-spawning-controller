#!/usr/bin/env python3
"""data_manifest.py -- pin the audited cue data by sha256 and verify it at container start.

    python scripts/data_manifest.py pin --audit-report <report.json>   # after a clean audit
    python scripts/data_manifest.py verify                             # what the containers run
    python scripts/data_manifest.py verify --data /data --scripts /app/scripts --status /data/data_manifest_check.json

The manifest (data/data_manifest.json) lists every file the cue plan is built from, with its sha256 and
size, plus the audit that passed when it was pinned (scripts/audit_cue_tables.py: date, commit, defects,
USNO sample and bound). Pinned:

    data/spawn_calendar_*.csv and .meta.json     the live tank calendar          (calendar's own hash check)
    scripts/cue_params.json                      the calendar's parameters      (baked into the image)
    data/reference/cue_params.json               the same, where cue_import.py installs it (public controller)
    data/locales/*/cue_table_*.csv               the 29 locale cue tables        audited
    data/locales/locale_profiles.csv             the arcs the watch flags use    audited
    data/external/oisst_locales/<id>.csv         raw SST the tables come from    audited (tier B)
    data/locales/*/temp_ladder_*.csv,            ladders beside the tables       pinned, not audited
      data/locales/*/light/*_light_ladder_daily.csv

Hashes are taken over the bytes with CRLF folded to LF, so a Windows checkout that flips line endings
does not count as a change; any other edit does.

verify exits 0 when every pinned file matches, 3 when any is changed, missing or (with --strict) new.
With DATA_MANIFEST_MODE=warn (the compose default) the container starts anyway and the result is
logged and written to the status file; with enforce the command fails, so the service does not start.
Re-pin after a deliberate change, once the audit is clean again.
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import hashlib
import json
import os
import subprocess
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = "data/data_manifest.json"
GROUPS = [  # (glob relative to the repo root, audited)
    ("data/spawn_calendar_*.csv", False),
    ("data/spawn_calendar_*.meta.json", False),
    ("scripts/cue_params.json", False),
    ("data/reference/cue_params.json", False),   # the public controller: installed by cue_import.py
    ("data/locales/*/cue_table_*.csv", True),
    ("data/locales/locale_profiles.csv", True),
    ("data/locales/*/temp_ladder_*.csv", False),
    ("data/locales/*/light/*_light_ladder_daily.csv", False),
]


def sha(path):
    with open(path, "rb") as fh:
        b = fh.read()
    b = b.replace(b"\r\n", b"\n")
    return hashlib.sha256(b).hexdigest(), len(b)


def resolve(rel, data, scripts):
    """Repo-relative path -> path on this host or in the container."""
    if rel.startswith("data/"):
        return os.path.join(data, rel[5:])
    if rel.startswith("scripts/"):
        return os.path.join(scripts, rel[8:])
    return os.path.join(REPO, rel)


def collect(data, scripts):
    files = {}
    for pat, audited in GROUPS:
        for p in sorted(glob.glob(resolve(pat, data, scripts))):
            rel = pat.split("/")[0] + "/" + os.path.relpath(p, data if pat.startswith("data/") else scripts).replace(os.sep, "/")
            files[rel] = audited
    # raw SST behind each audited cue table (tier B re-derives sst_clim_c from it)
    for rel in [r for r, a in files.items() if a and "/cue_table_" in r]:
        lid = rel.split("/")[2]
        raw = f"data/external/oisst_locales/{lid}.csv"
        if os.path.exists(resolve(raw, data, scripts)):
            files[raw] = True
    return files


def pin(a):
    files = collect(a.data, a.scripts)
    audit = None
    if a.audit_report:
        with open(a.audit_report, encoding="utf-8") as fh:
            rep = json.load(fh)
        if rep.get("n_defects"):
            sys.exit(f"refusing to pin: the audit report has {rep['n_defects']} defects")
        if any(b.get("flagged_locales") for b in rep.get("bias", {}).values()):
            sys.exit("refusing to pin: the audit report has bias flags")
        tc = rep.get("tier_c") or {}
        c_done = bool(tc.get("units")) and not tc.get("stopped")  # a stopped USNO sample does not count as tier C
        audit = {"report": os.path.basename(a.audit_report), "rows": rep.get("rows"), "locales": rep.get("locales"),
                 "tiers": rep.get("tiers") + ("C" if c_done else ""), "n_defects": rep.get("n_defects"),
                 "usno_units": tc.get("units"), "usno_upper95_defect_rate": tc.get("upper95_defect_rate")}
        if tc.get("stopped"):
            audit["usno_stopped"] = tc["stopped"]
    try:
        commit = subprocess.run(["git", "-C", REPO, "rev-parse", "--short", "HEAD"], capture_output=True,
                                text=True).stdout.strip()
    except OSError:
        commit = ""
    out = {"pinned_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "commit": commit,
           "audit": audit, "hash": "sha256 over the bytes with CRLF folded to LF",
           "files": {}}
    for rel, audited in sorted(files.items()):
        h, n = sha(resolve(rel, a.data, a.scripts))
        out["files"][rel] = {"sha256": h, "bytes": n, "audited": audited}
    path = a.manifest or resolve(MANIFEST, a.data, a.scripts)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(out, fh, indent=1)
        fh.write("\n")
    print(f"pinned {len(out['files'])} files ({sum(v['audited'] for v in out['files'].values())} audited) -> {path}")
    return 0


def verify(a):
    path = a.manifest or resolve(MANIFEST, a.data, a.scripts)
    mode = os.environ.get("DATA_MANIFEST_MODE", "warn").lower()
    res = {"checked_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "mode": mode,
           "manifest": os.path.basename(path), "ok": False, "changed": [], "missing": [], "new": []}
    if not os.path.exists(path):
        res["error"] = "no manifest"
    else:
        with open(path, encoding="utf-8") as fh:
            man = json.load(fh)
        res["pinned_utc"], res["commit"] = man.get("pinned_utc"), man.get("commit")
        for rel, want in man["files"].items():
            p = resolve(rel, a.data, a.scripts)
            if not os.path.exists(p):
                res["missing"].append(rel)
            elif sha(p)[0] != want["sha256"]:
                res["changed"].append(rel)
        res["new"] = sorted(set(collect(a.data, a.scripts)) - set(man["files"]))
        res["n_files"] = len(man["files"])
        res["ok"] = not res["changed"] and not res["missing"] and not (a.strict and res["new"])
    if a.status:
        try:
            with open(a.status, "w", encoding="utf-8") as fh:
                json.dump(res, fh, indent=1)
        except OSError as e:
            print(f"data_manifest: could not write {a.status}: {e}", file=sys.stderr)
    if res["ok"]:
        print(f"data_manifest: OK, {res['n_files']} pinned files match (pinned {res.get('pinned_utc')}, "
              f"{res.get('commit')})" + (f"; {len(res['new'])} new unpinned" if res["new"] else ""))
        return 0
    print("data_manifest: MISMATCH against the audited manifest"
          + (f" ({res['error']})" if res.get("error") else ""), file=sys.stderr)
    for k in ("changed", "missing", "new"):
        for rel in res[k]:
            print(f"  {k}: {rel}", file=sys.stderr)
    if mode == "enforce":
        print("data_manifest: DATA_MANIFEST_MODE=enforce, refusing to start", file=sys.stderr)
        return 3
    print("data_manifest: DATA_MANIFEST_MODE=warn, starting anyway", file=sys.stderr)
    return 0 if a.exit_zero_on_warn else 3


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("cmd", choices=["pin", "verify"])
    ap.add_argument("--data", default=os.path.join(REPO, "data"))
    ap.add_argument("--scripts", default=os.path.join(REPO, "scripts"))
    ap.add_argument("--manifest", help=f"default <data>/{MANIFEST[5:]}")
    ap.add_argument("--audit-report", help="pin: the clean audit_cue_tables.py JSON report")
    ap.add_argument("--status", help="verify: write the result here as JSON")
    ap.add_argument("--strict", action="store_true", help="verify: a new unpinned file is a mismatch")
    ap.add_argument("--exit-zero-on-warn", action="store_true",
                    help="verify: exit 0 on a mismatch unless DATA_MANIFEST_MODE=enforce (for startup)")
    a = ap.parse_args(argv)
    return pin(a) if a.cmd == "pin" else verify(a)


if __name__ == "__main__":
    sys.exit(main())
