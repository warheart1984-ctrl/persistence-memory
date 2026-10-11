#!/usr/bin/env python3
"""Anchor the call log's head in the backup anchors (issue #74).  Standard library plus app/call_log.py, read-only.

The call log is a hash chain, so editing a line is detectable, but anyone who can write its directory can rebuild the whole
chain, or cut trailing lines off, and it still verifies.  A head (seq + entry hash) kept OUTSIDE that directory, in every backup
set's anchors and in the offsite copies, catches both: a later log must still contain the entry that an earlier anchor named.

    calllog_anchor.py head  DIR                  print "calllog|<seq>|<entry_hash>" (nothing if there is no log); exit 3 if the chain has problems
    calllog_anchor.py check DIR [--own FILE]... ANCHORS_FILE...
                                                 every "calllog|seq|hash" line of the given anchors files must still be in DIR's chain.
                                                 A file given as --own is checked too, but never counts as a TRUSTED comparison: it was
                                                 produced from this very log, so it only proves the log matches itself.

It never writes, and it does not use CallLog.verify() because that flushes (it can append a gap entry), which would change the head.
Exit codes: 0 fine, 1 an anchored entry is missing or different (the log was truncated or rebuilt), 2 usage, 3 the chain itself is broken.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from app.call_log import GENESIS, entry_hash_of  # noqa: E402

FILE_RE = re.compile(r"^calls-(\d{8})\.jsonl$")
LINE_RE = re.compile(r"^calllog\|(\d+)\|([0-9a-f]{64})$")
SET_TS_RE = re.compile(r"^jarvis-(\d{8})T(\d{6})Z\.anchors$")
RETENTION_MARGIN_DAYS = 2


def walk(directory: Path):
    """Yield (seq, entry_hash) in order, and collect chain problems.  Returns (entries, problems, retention_anchor)."""
    problems: list[str] = []
    entries: dict[int, str] = {}
    prev_hash, prev_seq = GENESIS, 0
    anchor = None
    try:
        anchor = json.loads((directory / "anchor.json").read_text(encoding="utf-8"))
        prev_hash, prev_seq = str(anchor["entry_hash"]), int(anchor["seq"])
    except (OSError, ValueError, KeyError):
        anchor = None
    files = sorted((p for p in directory.iterdir() if FILE_RE.match(p.name)), key=lambda p: p.name) if directory.is_dir() else []
    for path in files:
        for n, line in enumerate(path.read_text(encoding="utf-8", errors="replace").split("\n"), start=1):
            if not line.strip():
                continue
            try:
                e = json.loads(line)
                seq, h, prev = int(e["seq"]), str(e["entry_hash"]), str(e["prev_hash"])
            except (ValueError, KeyError, TypeError):
                problems.append(f"{path.name}:{n} not a valid entry")
                continue
            if entry_hash_of(e) != h:
                problems.append(f"{path.name}:{n} entry {seq} was edited (hash does not match)")
            if seq != prev_seq + 1:
                problems.append(f"{path.name}:{n} entry {seq} follows {prev_seq} (a line was deleted, inserted or reordered)")
            if prev != prev_hash:
                problems.append(f"{path.name}:{n} entry {seq} does not link to the entry before it")
            entries[seq] = h
            prev_hash, prev_seq = h, seq
    return entries, problems, anchor


def retain_days() -> int:
    try:
        return max(1, int(os.environ.get("JARVIS_CALL_LOG_RETAIN_DAYS", "90")))
    except ValueError:
        return 90


def set_age(name: str) -> timedelta | None:
    """How old the backup set that wrote this anchors file is, from its own name (None if the name does not carry a time)."""
    m = SET_TS_RE.match(name)
    if not m:
        return None
    try:
        taken = datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return datetime.now(timezone.utc) - taken


def anchored(files: list[str]) -> list[tuple[str, str, int, str, timedelta | None]]:
    out = []
    for f in files:
        try:
            lines = Path(f).read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        out += [(f, Path(f).name, int(m.group(1)), m.group(2), set_age(Path(f).name)) for m in (LINE_RE.match(line) for line in lines) if m]
    return out


def main(argv: list[str]) -> int:
    if len(argv) < 3 or argv[1] not in ("head", "check"):
        print(__doc__, file=sys.stderr)
        return 2
    entries, problems, anchor = walk(Path(argv[2]))
    if argv[1] == "head":
        for p in problems:
            print(f"call log chain problem: {p}", file=sys.stderr)
        if entries:
            seq = max(entries)
            print(f"calllog|{seq}|{entries[seq]}")
        return 3 if problems else 0
    bad = 0
    for p in problems:
        print(f"call log chain problem: {p}", file=sys.stderr)
    head = max(entries) if entries else 0
    cutoff = timedelta(days=retain_days() + RETENTION_MARGIN_DAYS)
    matched = 0                                  # anchors found in the log with the same hash: the only trusted comparisons
    waived: list[str] = []
    own: set[str] = set()
    files: list[str] = []
    rest = iter(argv[3:])
    for a in rest:
        if a == "--own":
            f = next(rest, None)
            if f:
                own.add(f)
                files.append(f)
        else:
            files.append(a)
    for path, source, seq, h, age in anchored(files):
        if seq in entries:
            if entries[seq] != h:
                print(f"{source}: call log entry {seq} is not the one anchored (rebuilt or edited)", file=sys.stderr)
                bad = 1
            elif path not in own:
                matched += 1
        elif anchor and seq <= int(anchor["seq"]) and age is not None and age > cutoff:
            # Retention deletes whole files older than the window, so an anchor taken longer ago than that may legitimately be gone.
            # anchor.json sits in the directory an attacker can write, so it never excuses an anchor from INSIDE the window: a forged
            # high retention anchor would otherwise waive every genuine external check.
            if seq == int(anchor["seq"]) and str(anchor["entry_hash"]) != h:
                print(f"{source}: the retention anchor at seq {seq} differs from the one anchored", file=sys.stderr)
                bad = 1
            else:
                waived.append(source)
        elif seq > head:
            print(f"{source}: anchored head {seq} is beyond this log's head {head} (the log was truncated or is older)", file=sys.stderr)
            bad = 1
        else:
            print(f"{source}: call log entry {seq} is missing (the log was rebuilt or lines removed)", file=sys.stderr)
            bad = 1
    if waived and not matched:
        # every anchor handed in was old enough to be excused, so nothing trusted was compared at all (backups were interrupted for
        # longer than the retention window): a forged retention anchor could be hiding a rebuilt log, so do not excuse them
        print(f"{waived[0]}: no anchor inside the retention window could be compared with the log, so the old anchors cannot be excused", file=sys.stderr)
        bad = 1
    return 1 if bad else (3 if problems else 0)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
