# The drift watcher

An agent summarizes ledger records. When the summary is wrong (last night one credited Codex with a deploy Claude did, while the ledger was
right) only a human who read both would notice. The drift watcher checks every factual claim in a summary against the records it was written from,
and says which claims are **supported**, **contradicted** or **unsupported**. It never edits a record, and it uses **no model**: every verdict is a
deterministic rule over text, so the same input gives the same findings and the same `check_digest`.

## Use

```
POST /api/jarvis/drift/check          (operator only; the same check is the MCP tool emr_drift_check and POST /api/jarvis/tools/emr_drift_check)
{
  "summary": "<the agent's text, at most 20,000 characters>",
  "records": ["mem-...", ...],                   # the ids it cites / was served: 1 to 20
  "client": "devin",                             # optional: who wrote the summary (self-reported), to find its calls in the call log
  "served_digest": "<result_digest>",            # optional: the digest the agent says it was shown
  "served_content_sha256": {"mem-...": "<hash>"},# optional: the content hash the agent saw; a different one now is flagged
  "claims": [{"text": "...", "cites": ["mem-..."]}]   # optional: claims already extracted (for example by an LLM outside this server)
}
```

Over a limit (20,000 characters, 20 records, 200 claims) the request is **refused with 422 and the numbers**, never quietly shortened. Nothing is
checked in that case.

The response lists every claim with `status` (`supported` | `contradicted` | `unsupported`), a `reason`, the findings (the record, the field, the value
claimed and the value found, with the evidence text), whether the claim was `hedged`, and any quotation that was not checked; then `skipped` clauses
(questions, opinions, fragments: counted, never silently dropped), `warnings`, the records as read, the `call_log` matches, and:

* `verdict`: `drift` (any claim contradicted), `unverified` (nothing contradicted, but something unsupported), `pass` (everything supported);
* `check_digest`: sha256 of the canonical JSON of the summary hash, the records' ids, versions and content hashes, and the claim results.

**Unsupported does not mean false.** It means the cited records cannot confirm the claim. **Contradicted** means a record field disagrees, and the
finding names it.

## What is checked

| Family | Checked against | Reasons |
|---|---|---|
| Actor ("Codex deployed it") | `source_agent` answers "who **wrote** the record"; the content answers "who **did** the action". A record that names nobody gives only authorship, so the claim is *unsupported*, not supported | `ACTOR_CONTRADICTED`, `ACTOR_AUTHORSHIP_ONLY` |
| Status / lifecycle | "draft/verified/archived" bind to the stored `status`; "active/superseded" bind to the derived `lifecycle`; "status ... active" binds to `status`. A status phrase is checked against the record named just before it, else against the records in scope | `STATUS_CONTRADICTED`, `LIFECYCLE_CONTRADICTED` |
| Supersede direction | `supersedes` and `superseded_by`; understands ids, "the old/new record" and "Claude's record" | `SUPERSEDE_DIRECTION` |
| Commits, hashes, dates | whole-token match in content, subject, evidence and `created_at`; contradicted only when the same slot ("main <sha>", "on <date>") holds a different value | `TOKEN_MISMATCH`, `TOKEN_UNSUPPORTED` |
| Numbers | the same number must appear in the record; contradicted when the same slot word has a different number ("schema 8" vs "schema 7 to schema 9") | `NUMBER_MISMATCH`, `NUMBER_UNSUPPORTED` |
| State claims | a small explicit rule table (`STATE_RULES` in `app/drift.py`): signatures live/enabled, twin enabled, against record text such as "shelved", "warn", "no trust roots", "404" | `STATE_CLAIM_CONTRADICTED`, `STATE_CLAIM_UNSUPPORTED` |
| Narrator-style | an id named but not submitted; an unknown record; an entity the records never mention; an absolute word ("always", "proves"); words in the clause that no cited record contains | `CITE_MISSING`, `RECORD_NOT_FOUND`, `ENTITY_UNSUPPORTED`, `CLAIM_WORD`, `UNSUPPORTED_TEXT` |

The technique (clause-level checks, whole-word matching, the drop-reason vocabulary) is borrowed from the twin's narrator gate. This is new code for
a different input (free text plus record ids, with no typed state to cite into); nothing is imported from the twin repository. A hedged claim
("probably live") is still checked and marked `hedged`. Quoted text is not checked: a quotation is not something the author asserts.

## What was the agent actually served?

The watcher reads the records **as they are now**. The call log can show that a call happened: for `emr_fetch` it proves a call with exactly
`{"id": ...}` (the logged argument hash matches), and for `emr_latest` and search it matches the `result_digest`. **A match proves a call and its
hashes, not the text the agent was shown.** If a record changed since, pass `served_content_sha256` and the watcher flags
`RECORD_CHANGED_SINCE_SERVED`. Reconstructing a record as it was at the call (through the replay history) is a follow-up.

## The drift log (not the ledger)

Every check appends one entry to a separate, append-only, hash-chained log: the call log's machinery with the file prefix `drift`
(`drift-YYYYMMDD.jsonl`, in `JARVIS_DRIFT_LOG_DIR`, default `drift-log/` next to the store). Why not ledger records: a ledger write would change the
state root and history on every check (a read-only checker would become a writer); Clause V says the ledger stores evidence, and findings are
derived commentary on an agent's text; and machine output would add noise to recall. An entry holds the summary's hash and length, the verdict,
counts, the `check_digest`, the record ids, the matched call-log seqs, the tenant, the self-reported client, and the **contradicted or unsupported
clauses** (each truncated to 300 characters). It never holds the whole summary, and never supported or skipped text. If the log cannot be written the
verdict is still returned (the ledger is not at stake) and the log is flagged degraded.

Operator-only readers: `GET /api/jarvis/drift/log` and `GET /api/jarvis/drift/log/verify`.

## Access and the flag

Operator only: an OAuth tenant gets `403 AUTHORITY_DENIED` on every surface. Records are read through the normal tenant-scoped store, so a record
that belongs to another tenant is "not found". `JARVIS_DRIFT_CHECK_ENABLED` is on in dev and test and **off when `JARVIS_ENV=production`**; a deploy
turns it on explicitly. The check also appears in the call log like any other call.

## What it cannot catch

Said plainly, because a green result must not be read as more than it is:

* **Paraphrase and anything outside the rule table.** "The assistant that runs on the operator's PC ran it" will not map to an agent name. A claim worded
  differently from the record is `unsupported`, not "supported by meaning".
* **Claims about things not in the cited records.** They come back `unsupported`; the watcher cannot disprove them.
* **Misleading omissions** and true-but-selective summaries.
* **Pronouns and relative times** ("it", "yesterday", "last night") are not resolved.
* **Quoted material** is not checked, so a wrong claim inside quotation marks passes through.
* **Editorial commentary** ("notable", "went") shows as `unsupported` noise.
* **What the agent actually saw.** See above: the call log proves a call and its hashes, not the text.
* **Records changed since the call**, unless the submitter supplies the content hash.
* **A summary that cites no ids and names no recognizable entity** gives the checker little to bind to.
* **It is built to avoid false alarms (contradicted needs a same-slot mismatch) and so it misses subtler errors.** `unsupported` is where such misses
  hide, so read it as "not confirmed", not "fine".
* The state rules cover signatures and the twin only; other state claims are `unsupported`.
* The call log and the drift log can be rewritten by anyone who can write their directory; `verify` returns the head hash so it can be kept elsewhere.

## Configuration

| Variable | Default | |
|---|---|---|
| `JARVIS_DRIFT_CHECK_ENABLED` | on in dev and test; **off when `JARVIS_ENV=production`** | the deploy turns it on explicitly |
| `JARVIS_DRIFT_LOG_DIR` | `<dir of JARVIS_STORE_PATH>/drift-log` | |

Demo (read-only, against the live records): `python scripts/drift_demo.py --key-file <file>`.
