#!/usr/bin/env python3
"""Turn the text of a backup's data (``pg_restore --data-only -f - -n jarvis < set.dump``) into the signatures export that rides with
every backup set (``<set>.signatures.json``): the attestations, the trust statements, the blocks' hashes and the receipts' ids.

It is read from the dump itself, so the export always matches the dump it sits next to.  Standard library only: it runs on the box
and on the PC.  The witness tool (``python -m app.witness``) verifies this file with nothing but the pinned root public keys.
"""

from __future__ import annotations

import json
import re
import sys

WANTED = {
    "attestations": ("signer_seq",),
    "trust_statements": ("stmt_seq", "arg"),
    "blocks": ("height",),
    "evidence_objects": (),
}
RECEIPT_SCHEMA = "CES.Local.ReplayReceipt.v1"
_HEADER = re.compile(r"^COPY (?:[A-Za-z0-9_\"]+\.)?\"?([A-Za-z0-9_]+)\"? \(([^)]*)\) FROM stdin;$")
_ESCAPES = {"b": "\b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v", "\\": "\\"}


def unescape(field: str) -> str | None:
    """COPY text format: ``\\N`` is NULL; backslash escapes for control characters and backslash itself."""
    if field == "\\N":
        return None
    if "\\" not in field:
        return field
    out, i = [], 0
    while i < len(field):
        c = field[i]
        if c == "\\" and i + 1 < len(field):
            n = field[i + 1]
            out.append(_ESCAPES.get(n, n))
            i += 2
        else:
            out.append(c)
            i += 1
    return "".join(out)


def parse(lines) -> dict[str, list[dict]]:
    tables: dict[str, list[dict]] = {t: [] for t in WANTED}
    current: str | None = None
    columns: list[str] = []
    for raw in lines:
        line = raw.rstrip("\n")
        if current is None:
            m = _HEADER.match(line)
            if m and m.group(1) in WANTED:
                current = m.group(1)
                columns = [c.strip().strip('"') for c in m.group(2).split(",")]
            continue
        if line == "\\.":
            current = None
            continue
        values = [unescape(f) for f in line.split("\t")]
        row = dict(zip(columns, values))
        for key in WANTED[current]:
            if row.get(key) is not None:
                row[key] = int(row[key])
        tables[current].append(row)
    return tables


def build(tables: dict[str, list[dict]]) -> dict:
    tenants: dict[str, dict] = {}

    def slot(t: str) -> dict:
        return tenants.setdefault(t, {"statements": [], "attestations": [], "blocks": [], "receipts": []})

    for r in tables["trust_statements"]:
        slot(r["tenant_key"])["statements"].append({k: r.get(k) for k in (
            "stmt_seq", "kind", "key_id", "pubkey", "arg", "subject_hash", "prev_hash", "signed_by", "signature", "statement_hash")})
    for r in tables["attestations"]:
        slot(r["tenant_key"])["attestations"].append({k: r.get(k) for k in (
            "signer_seq", "kind", "subject", "subject_hash", "prev_hash", "key_id", "signed_at", "signature", "attestation_hash")})
    for r in tables["blocks"]:
        slot(r["tenant_key"])["blocks"].append({"height": r["height"], "block_hash": r["block_hash"]})
    for r in tables["evidence_objects"]:
        if r.get("schema_id") == RECEIPT_SCHEMA:
            slot(r["tenant_key"])["receipts"].append(r["id"])
    for t in tenants.values():
        t["statements"].sort(key=lambda x: x["stmt_seq"])
        t["attestations"].sort(key=lambda x: x["signer_seq"])
        t["blocks"].sort(key=lambda x: x["height"])
        t["receipts"].sort()
    return {"format": 1, "tenants": dict(sorted(tenants.items()))}


def main() -> int:
    export = build(parse(sys.stdin))
    json.dump(export, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
