#!/usr/bin/env python3
"""Checks on code-list values that validate_pack.py does not make.

    python scripts/ci/check_pack_lists.py                         # every finding fails
    python scripts/ci/check_pack_lists.py --baseline <checkout>   # only new findings fail

validate_pack.py checks that a pack is well-formed: codes, references, schemas.
A list can pass it and still be unusable in a dropdown, and that is what this
looks for in every packs/*/codelists/*.json and packs/*/domains/*/*.json:

  - a value with no display name, or one that is only a prefix ("Rapeseed –")
  - a display name cut off by its source ("Deka (ETBW …")
  - unbalanced brackets, usually the same truncation
  - Ethiopic script in value_display: a translation belongs in display_i18n
  - two values that read the same in one dropdown. Values are compared within
    their scope: the same parent, and the same value of every attribute that
    points at another list (x-list-ref), so "Afar" may be both a cattle and a
    sheep breed, but not two cattle breeds

These are found after the fact in data already merged, so --baseline compares
against another checkout (a PR's base) and fails only on findings the change
introduces; the rest are listed, so they stay visible until someone fixes them.

It also checks that packs/ETH-OAN matches scripts/packs/eth_oan_additions: an
addition that is not in the built pack (for attributes/, a list whose schema or
value attributes differ from the overlay, for displays/, a value whose shown name
differs) means build_eth_oan_pack.py was not run,
and a pack edited by hand loses that edit at the next rebuild. Those findings
always fail.

Exits non-zero on failure, so it works as a CI gate.
"""

from __future__ import annotations

import argparse
import collections
import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
ADDITIONS = "scripts/packs/eth_oan_additions"
BUILT = "packs/ETH-OAN"

ETHIOPIC = re.compile(r"[ሀ-᎟ⶀ-⷟꬀-꬯]")
# A display ending in a separator lost whatever followed it.
DANGLING = re.compile(r"[–—\-:/,(]\s*$")


def tracked(root, *patterns):
    """Files git tracks, so a stray local output never fails the build."""
    out = subprocess.run(["git", "ls-files", "--", *patterns], cwd=root,
                         check=True, capture_output=True, text=True).stdout
    return sorted(p for p in out.splitlines() if p)


def read_json(root, rel):
    with open(os.path.join(root, rel), encoding="utf-8") as fh:
        return json.load(fh)


def list_files(root):
    return tracked(root, "packs/*/codelists/*.json", "packs/*/domains/*/*.json")


def scope(doc, value):
    """What a value is told apart within: its parent and every list it points at."""
    props = (doc.get("attribute_schema") or {}).get("properties") or {}
    refs = sorted(p for p, s in props.items() if isinstance(s, dict) and "x-list-ref" in s)
    attrs = value.get("attributes") or {}
    return (value.get("parent_value_code"),) + tuple(attrs.get(p) for p in refs)


def findings(root):
    """{key: message}; the key is stable across checkouts, so two runs can be compared."""
    out = {}
    for rel in list_files(root):
        doc = read_json(root, rel)
        pack = rel.split("/")[1]
        list_id = doc.get("attribute_id") or os.path.splitext(os.path.basename(rel))[0].upper()
        values = doc.get("values") or []

        def add(value_id, kind, detail, rel=rel, pack=pack, list_id=list_id):
            out[f"{pack}/{list_id}/{value_id}/{kind}"] = (rel, f"{pack} {list_id} {value_id}: {detail}")

        for v in values:
            vid, shown = v.get("value_id"), (v.get("value_display") or "").strip()
            if not shown:
                add(vid, "empty", "no display name")
                continue
            if "…" in shown or "..." in shown:
                add(vid, "truncated", f"display name cut off: {shown!r}")
            elif DANGLING.search(shown):
                add(vid, "dangling", f"display name ends in a separator, the rest is missing: {shown!r}")
            if shown.count("(") != shown.count(")"):
                add(vid, "brackets", f"unbalanced brackets: {shown!r}")
            if ETHIOPIC.search(shown):
                add(vid, "script", f"Ethiopic in value_display, move it to display_i18n.am: {shown!r}")

        same = collections.defaultdict(list)
        for v in values:
            shown = (v.get("value_display") or "").strip().casefold()
            if shown:
                same[(scope(doc, v), shown)].append(v.get("value_id"))
        for (_, shown), ids in same.items():
            if len(ids) > 1:
                for vid in ids:
                    others = ", ".join(i for i in ids if i != vid)
                    add(vid, "duplicate", f"reads the same as {others} in one dropdown: {shown!r}")
    return out


def additions_errors(root):
    """packs/ETH-OAN must be what build_eth_oan_pack.py makes of the additions."""
    errors = []
    if not os.path.isdir(os.path.join(root, ADDITIONS)) or not os.path.isdir(os.path.join(root, BUILT)):
        return errors
    for rel in tracked(root, f"{ADDITIONS}/*.json"):
        sub = os.path.relpath(rel, ADDITIONS).replace(os.sep, "/")
        if sub.startswith("values/"):
            target = f"{BUILT}/{sub[len('values/'):]}"
            if not os.path.exists(os.path.join(root, target)):
                errors.append((rel, f"{rel}: adds values to {target}, which does not exist"))
                continue
            built = {v["value_id"]: v for v in read_json(root, target)["values"]}
            for v in read_json(root, rel)["values"]:
                if built.get(v["value_id"]) != v:
                    errors.append((rel, (f"{rel}: {v['value_id']} differs from {target} or is missing "
                                         f"(run build_eth_oan_pack.py)")))
        elif sub.startswith("attributes/"):
            target = f"{BUILT}/{sub[len('attributes/'):]}"
            if not os.path.exists(os.path.join(root, target)):
                errors.append((rel, f"{rel}: sets attributes on {target}, which does not exist"))
                continue
            overlay, built = read_json(root, rel), read_json(root, target)
            if built.get("attribute_schema") != overlay.get("attribute_schema"):
                errors.append((rel, f"{rel}: attribute_schema differs from {target} (run build_eth_oan_pack.py)"))
            by_code = {v.get("value_code") or v["value_id"]: v for v in built["values"]}
            for code, attributes in (overlay.get("values") or {}).items():
                if (by_code.get(code) or {}).get("attributes") != attributes:
                    errors.append((rel, (f"{rel}: attributes of {code} differ from {target} or the value is "
                                         f"missing (run build_eth_oan_pack.py)")))
        elif sub.startswith("displays/"):
            target = f"{BUILT}/{sub[len('displays/'):]}"
            if not os.path.exists(os.path.join(root, target)):
                errors.append((rel, f"{rel}: renames values in {target}, which does not exist"))
                continue
            built = {v["value_id"]: v for v in read_json(root, target)["values"]}
            for value_id, display in (read_json(root, rel).get("values") or {}).items():
                if (built.get(value_id) or {}).get("value_display") != display:
                    errors.append((rel, (f"{rel}: {value_id} does not read {display!r} in {target} or is "
                                         f"missing (run build_eth_oan_pack.py)")))
        else:
            target = f"{BUILT}/{sub}"
            if not os.path.exists(os.path.join(root, target)):
                errors.append((rel, f"{rel}: not in the built pack as {target} (run build_eth_oan_pack.py)"))
            elif read_json(root, rel) != read_json(root, target):
                errors.append((rel, (f"{rel}: differs from {target}; edit the addition and rebuild, "
                                     f"never the built file")))
    return errors


def annotate(level, rel, message):
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::{level} file={rel}::{message}")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--baseline", help="another checkout (e.g. the PR's base); only findings it lacks fail")
    args = ap.parse_args()

    head = findings(ROOT)
    base = findings(os.path.abspath(args.baseline)) if args.baseline else {}
    new = {k: v for k, v in head.items() if k not in base}
    known = {k: v for k, v in head.items() if k in base}
    fixed = [k for k in base if k not in head]

    for rel, msg in sorted(known.values(), key=lambda x: x[1]):
        print(f"KNOWN {msg}")
    for rel, msg in sorted(new.values(), key=lambda x: x[1]):
        print(f"NEW   {msg}")
        annotate("error", rel, msg)
    errors = additions_errors(ROOT)
    for rel, msg in errors:
        print(f"ERROR {msg}")
        annotate("error", rel, msg)

    kinds = collections.Counter(k.rsplit("/", 1)[1] for k in head)
    print(f"checked {len(list_files(ROOT))} lists: {len(head)} finding(s) {dict(sorted(kinds.items()))}; "
          f"{len(new)} new, {len(known)} already on the baseline, {len(fixed)} fixed; "
          f"{len(errors)} ETH-OAN additions error(s)")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as fh:
            fh.write(f"### Pack lists\n\n| | count |\n|---|---|\n| new findings (fail) | {len(new)} |\n"
                     f"| already on the base branch | {len(known)} |\n| fixed by this change | {len(fixed)} |\n"
                     f"| ETH-OAN additions errors (fail) | {len(errors)} |\n\n"
                     f"By kind: {dict(sorted(kinds.items()))}\n")
    sys.exit(1 if new or errors else 0)


if __name__ == "__main__":
    main()
