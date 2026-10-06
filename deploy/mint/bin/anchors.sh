#!/usr/bin/env bash
# Chain-head anchors: a small text record of where each tenant's history chain ends, kept OUTSIDE the
# database (next to every backup and in every offsite copy). If the database is later rolled back or its
# history rewritten, the anchors no longer line up. This is the interim, scripted form of the
# "export chain-head hashes outside the database" follow-up; see docs/POSTGRES.md.
#
# Canonical line formats (sorted, '|'-separated):
#   head|<tenant>|<record id>|<last_seq>|<last_hash>|<deleted t/f>
#   counter|<tenant>|<last_seq>
#   block|<tenant>|<height>|<last_seq>|<block_hash>        (schema v6+: every sealed Continuity Block)
# shellcheck shell=bash

# anchors_from_dump FILE : read straight out of a pg_dump -Fc file (so it always matches the dump).
anchors_from_dump() {
  pg_exec pg_restore --data-only -f - -n jarvis < "$1" | awk -F'\t' '
    /^COPY jarvis\.chain_heads / { mode = "head"; next }
    /^COPY jarvis\.history_counters / { mode = "counter"; next }
    /^COPY jarvis\.blocks / { mode = "block"; next }
    /^COPY / { mode = ""; next }
    /^\\\.$/ { mode = ""; next }
    mode == "head" && NF >= 5    { print "head|" $1 "|" $2 "|" $3 "|" $4 "|" $5 }
    mode == "counter" && NF >= 2 { print "counter|" $1 "|" $2 }
    # blocks columns: tenant_key height first_seq last_seq entry_count prev_block_hash entries_root block_hash ...
    mode == "block" && NF >= 8   { print "block|" $1 "|" $2 "|" $4 "|" $8 }
  ' | LC_ALL=C sort
}

# anchors_collect RUNNER : the anchor lines of a live database. RUNNER is a command that runs psql, with the
# arguments it is given, as a role that RLS does not bind (the postgres superuser). A database older than schema v6
# has no blocks table and simply contributes no block lines.
anchors_collect() {
  local run="$1"
  {
    "$run" -c "COPY (SELECT 'head', tenant_key, id, last_seq, last_hash, deleted FROM jarvis.chain_heads) TO STDOUT WITH (FORMAT text, DELIMITER '|')"
    "$run" -c "COPY (SELECT 'counter', tenant_key, last_seq FROM jarvis.history_counters) TO STDOUT WITH (FORMAT text, DELIMITER '|')"
    if [ "$("$run" -c "select to_regclass('jarvis.blocks') is not null")" = "t" ]; then
      "$run" -c "COPY (SELECT 'block', tenant_key, height, last_seq, block_hash FROM jarvis.blocks) TO STDOUT WITH (FORMAT text, DELIMITER '|')"
    fi
  } | LC_ALL=C sort
}

anchors_live_psql() { pg_exec psql -X -At -d jarvis "$@"; }

# anchors_from_db : read from the running database (as the postgres superuser, which RLS does not bind).
anchors_from_db() { anchors_collect anchors_live_psql; }

# anchors_check PREVIOUS NEW : exit 1 and print a line per problem if NEW is not a legitimate successor.
# Heads may advance (new last_seq) or stay identical; they may never disappear, move backwards, or change
# their hash without advancing. Counters may only grow. Every block that was anchored must still exist with the
# very same hash: a block can be added, never removed and never re-sealed (that is what catches a removed newest
# block, or a rewritten history whose blocks were all re-sealed consistently - neither is visible inside the database).
anchors_check() {
  awk -F'|' -v prev="$1" '
    FILENAME == prev {
      if ($1 == "head")    { ps[$2 "|" $3] = $4; ph[$2 "|" $3] = $5 }
      if ($1 == "counter") { pc[$2] = $3 }
      if ($1 == "block")   { pb[$2 "|" $3] = $5 }
      next
    }
    $1 == "head"    { ns[$2 "|" $3] = $4; nh[$2 "|" $3] = $5 }
    $1 == "counter" { nc[$2] = $3 }
    $1 == "block"   { nb[$2 "|" $3] = $5 }
    END {
      bad = 0
      for (k in ps) {
        if (!(k in ns))          { print "anchor problem: record " k " vanished from the chain heads"; bad = 1; continue }
        if (ns[k] + 0 < ps[k] + 0) { print "anchor problem: record " k " moved BACKWARDS (seq " ps[k] " -> " ns[k] ")"; bad = 1 }
        else if (ns[k] == ps[k] && nh[k] != ph[k]) { print "anchor problem: record " k " changed its hash without advancing"; bad = 1 }
      }
      for (t in pc) {
        if (!(t in nc))            { print "anchor problem: tenant " t " counter vanished"; bad = 1 }
        else if (nc[t] + 0 < pc[t] + 0) { print "anchor problem: tenant " t " counter went BACKWARDS (" pc[t] " -> " nc[t] ")"; bad = 1 }
      }
      for (k in pb) {
        if (!(k in nb))          { print "anchor problem: block " k " vanished (a block was removed)"; bad = 1 }
        else if (nb[k] != pb[k]) { print "anchor problem: block " k " changed its hash (the blocks were re-sealed)"; bad = 1 }
      }
      exit bad
    }
  ' "$1" "$2"
}

# anchor_file_sha FILE : hash that the NEXT anchors file records, chaining the files together so that
# editing an old anchors file on disk is detected.
anchor_file_sha() { sha256sum "$1" | cut -d' ' -f1; }

# anchors_verify_chain DIR : every anchors-<ts>.txt records the sha256 of the file before it; a mismatch means
# an old anchors file was edited or removed. (Files are only written when the anchors change, and never pruned.)
anchors_verify_chain() {
  local dir="$1" prev="" recorded f
  for f in $(ls -1 "$dir"/anchors-*.txt 2>/dev/null | LC_ALL=C sort); do
    recorded="$(sed -n 's/^# prev_sha256=//p' "$f" | head -1)"
    if [ -z "$prev" ]; then
      [ "$recorded" = "0" ] || { echo "first anchors file does not start the chain: $f" >&2; return 1; }
    else
      [ "$recorded" = "$(anchor_file_sha "$prev")" ] || { echo "chain broken before $f" >&2; return 1; }
    fi
    prev="$f"
  done
  return 0
}
