# Six-agent relay test on the call-log build, 2026-10-11

Generated 2026-10-11T02:04:37Z by `scripts/relay_evidence.py` against `http://127.0.0.1:8011` (read-only (GET reads and read-only tool POSTs only)).

## Summary

After the app-only deploy of main (status/lifecycle fix and the server-side call log) the operator restarted the MCP connection in each agent and had each one call emr_latest with no arguments. This report has two parts that must not be mixed up. The results table and the call-log section are what scripts/relay_evidence.py itself read from the live ledger (read-only; the API key is never printed): the ledger's records and chain, and the SERVER's own log of which clients called emr_latest, when, over which transport, with which result_digest, and whether they passed arguments. Client names in that log are self-reported by the clients. The agent table is what the agents told the operator; for this run no agent output was pasted, so every entry says so and nothing from agents is counted as verified. Expected: every witnessed emr_latest call returns result_digest 3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae, the digest of the new response shape (it now covers each record's stored status and its lifecycle, so it differs from the 8d3d2abb... and e1e2f0da... values of the earlier run).

## What the ledger looked like when read

- History seq: **136**, records: **61**
- State root: `78cb9ced870b551e4e00dd7e8258c68a5ef0dd659a2db99519e0bbb2b2b14537`
- Ledger head: `block:36a22998920174d104bc500696ff6c21547cf9e50dad546f06ea661557336ce0`
- `emr_latest` (no arguments) `result_digest`: `3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae`

## Results (read and re-run by the script)

| Result | Check | Detail |
|---|---|---|
| **PASS** | `writer_of_old_record_is_codex` | mem-1dc144c193a1 source_agent='codex' |
| **PASS** | `writer_of_new_record_is_claude` | mem-c42330528ad5 source_agent='claude' |
| **PASS** | `supersede_link_is_correct` | mem-c42330528ad5.supersedes='mem-1dc144c193a1'; mem-1dc144c193a1.superseded_by='mem-c42330528ad5'; old is superseded=True |
| **PASS** | `newest_record_is_expected` | emr_latest newest='mem-c42330528ad5', expected 'mem-c42330528ad5' (point in time) |
| **PASS** | `digest_re_derives` | sha256 over [id, created_at, status, lifecycle] of the 10 returned records = 3bed25d21d893e8a…; server said 3bed25d21d893e8a… |
| **PASS** | `superseded_record_hidden_by_default` | mem-1dc144c193a1 in default list: False; in include_superseded list: True |
| **PASS** | `chain_verifies` | history ok=True problems=[]; blocks ok=True problems=[] |
| **PASS** | `old_receipt_re_derives` | receipt verify ok=True problems=[] |
| **PASS** | `history_seq_135_136_are_the_two_records` | seq 135 -> 'mem-1dc144c193a1' ('create'); seq 136 -> 'mem-c42330528ad5' ('create') |
| **NOT RUN** | `agent_digest_reproduces_from_seq_135_state` | no agent-reported digest in the inputs file |
| **PASS** | `call_log_chain_verifies` | call log entries=54 head seq=54 problems=[] |

All checks passed: **True**

## Known gaps and deviations

- The call log has existed only since the deploy (about 00:18Z on 2026-10-11); nothing before that was witnessed by the server. The earlier relay run (2026-10-10) can only be compared through the agents' own reports.
- Client names are self-reported. A name in the log shows what the client called itself, not who was behind it. 'rmcp/3.1.0' (a Rust MCP client library name) is attributed to Devin by the operator ('that was devin'); the server log cannot confirm that. The attribution is corroborated, not proven: the pasted Devin transcript shows emr_latest returning the ten newest records and then a fetch of the decision, and the log has rmcp/3.1.0 calling emr_latest with no arguments (digest 3bed25d2...) at 00:58:01Z and fetch at 00:58:24Z. Its earlier calls at 00:42Z (emr_latest with {"limit": 5}, then fetch) are attributed on the same basis.
- Cursor's emr_latest call (seq 13) and the 'rmcp/3.1.0' client's emr_latest call (seq 14) were not made with no arguments: the argument hash in the log equals the hash of exactly {"limit": 5}, so both asked for five records and correctly got a different digest (018d59bdeaaeb4976e97c73051fc5c1dffefa9c284df380f0b2ae4e59e0ee93a), the same for both. The OpenCode (seq 8), Codex (seq 10 and 11) and Claude (seq 1) calls used no arguments and returned 3bed25d2.... The log keeps only a hash of the arguments, never the arguments themselves.
- Neither Devin nor Kilo appears in the log under its own name: Devin's MCP client identifies itself as 'rmcp/3.1.0' (so the report matches it through an alias, on the operator's attribution), and Kilo's calls came from 'Python-urllib/3.12' over plain HTTP, not the MCP bridge. Calls recorded as 'Python-urllib' carry no agent name; some are this report's own earlier dry run, the rest cannot be attributed by the log.
- 'Newest record is mem-c42330528ad5' is a point-in-time check; the report records the history seq and the call-log head it was read at.
- The self-heal restart that happened during the previous (v8/v9) deploy did not recur in this deploy: the pause flag was held until the new app was healthy, and the alerts log has no entry since the deploy started.
- The call log's head hash printed in this report should be copied off the box, and into the next ledger record that is written (anyone who can write the log directory can rebuild a whole chain; trailing entries can be truncated without breaking it).
- Calls from 'Python-urllib/3.12' (seq 2-7 before this run's reruns, and seq 23-24 at 00:56Z) are plain HTTP, not the MCP bridge, and carry no agent name. The operator attributes seq 23-24 to Kilo. Kilo's MCP connection works ('connected' in its own status) but was not the path used. To make such calls attributable, a script can send the header X-Jarvis-MCP-Client: <agent>/<version> (self-reported, like every client name here).

## Agent results: reported by agent, NOT verified by the script

These are what each agent said it saw, as pasted by the operator. The script did not run, observe or confirm any of them.

| Agent | Status of this entry | Newest id reported | Digest reported | Notes |
|---|---|---|---|---|
| Devin | operator-attested; corroborated by the pasted transcript | `-` | `-` | After the operator said 'that was devin', the client 'rmcp/3.1.0' is counted as Devin. Server-witnessed: emr_latest with no arguments at 00:58:01Z (digest 3bed25d2...) and fetch at 00:58:24Z, over mcp-stdio, matching the transcript Devin produced (the ten newest records, ledger head block:36a22998..., then the decision mem-c42330528ad5 with lifecycle active). Earlier, at 00:42Z, the same client called emr_latest with {"limit": 5} and fetch. The name 'rmcp' is a library name, so the attribution is the operator's statement, not the server's. |
| OpenCode | no output pasted for this run | `-` | `-` | As above. Its checkout on the PC was updated and its MCP connection restarted before this run (per the operator). |
| Codex | no output pasted for this run | `-` | `-` | As above. |
| Cursor | no output pasted for this run | `-` | `-` | As above. (No Cursor output was supplied in the earlier run either.) |
| Kilo | operator says Kilo made calls; the server log cannot attribute them | `-` | `-` | Kilo's own CLI reports 'continuity-ledger connected' (the MCP server starts), and no project or global config overrides it. Kilo's first rerun did not use the MCP: it fetched /openapi.json over HTTP at 00:54:04Z. After a second rerun the log shows emr_latest (no arguments, digest 3bed25d2...) at 00:56:28Z and emr_fetch at 00:56:34Z, both from client 'Python-urllib/3.12' over transport http-tool, i.e. a Python script talking HTTP, not the MCP bridge. The operator attributes those calls to Kilo; the log cannot, because 'Python-urllib' is a library name that any script uses. The calls are server-witnessed; who made them is the operator's statement, not the server's. |
| Claude | own call | `mem-c42330528ad5` | `-` | Claude has no MCP server configured on the PC, so its call is a curl from the Mint box with a self-reported client header (claude-code/deploy-check), made at the deploy check. |

## Server-witnessed calls to emr_latest (client names are self-reported)

Call log chain verifies: **True** (54 entries in 1 file(s)). **Head: seq 54, hash `7d06a2da15dfb2da9dc8c42f0360e6f17d83854d281b648ae4a9f174dc61720f`**. Record this off the box, and in the next ledger record that is written.

| Agent | Witnessed emr_latest calls | Latest: seq, time, transport | Client as it reported itself | result_digest | Called with no arguments | Outcome |
|---|---|---|---|---|---|---|
| Devin | 2 | 25, 2026-10-11T00:58:01.216899Z, mcp-stdio | rmcp/3.1.0 | `3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae` | yes | ok |
| OpenCode | 1 | 8, 2026-10-11T00:39:18.759626Z, mcp-stdio | opencode/1.18.35 | `3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae` | yes | ok |
| Codex | 2 | 11, 2026-10-11T00:40:19.365844Z, mcp-stdio | codex-mcp-client/0.162.0-alpha.17.2 | `3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae` | yes | ok |
| Cursor | 1 | 13, 2026-10-11T00:41:43.513008Z, mcp-stdio | cursor-vscode/1.0.0 | `018d59bdeaaeb4976e97c73051fc5c1dffefa9c284df380f0b2ae4e59e0ee93a` | no | ok |
| Kilo | 0 | - | - | - | - | no emr_latest call from a client whose name contains any of 'kilo' is in the part of the server's log that was read (it may have called under another name, before the log existed, or not at all: see the list of every client name the log saw) |
| Claude | 1 | 1, 2026-10-11T00:18:27.357524Z, http-tool | claude-code/deploy-check | `3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae` | yes | ok |

Every client name the log saw (coverage: complete: all 54 log entries (1 page(s)) were read; self-reported, so a name is a claim and not an identity). The last column says whether it was counted for one of the agents above. Rows named `relay-evidence` are this report's own read-only calls:

| Client name / version | Transport | Calls | Tools | Last seen | Matched an agent above |
|---|---|---|---|---|---|
| relay-evidence/1 | http-tool | 33 | emr_latest | 2026-10-11T02:04:37.025948Z | **no** |
| Python-urllib/3.12 | http-tool | 8 | emr_fetch, emr_latest | 2026-10-11T00:56:34.706749Z | **no** |
| codex-mcp-client/0.162.0-alpha.17.2 | mcp-stdio | 4 | emr_latest, emr_recall | 2026-10-11T00:40:19.532772Z | yes |
| rmcp/3.1.0 | mcp-stdio | 4 | emr_latest, fetch | 2026-10-11T00:58:24.158705Z | yes |
| curl/8.5.0 | http-api | 2 | POST /api/jarvis/blocks/seal | 2026-10-11T01:55:56.193101Z | **no** |
| claude-code/deploy-check | http-tool | 1 | emr_latest | 2026-10-11T00:18:27.357524Z | yes |
| cursor-vscode/1.0.0 | mcp-stdio | 1 | emr_latest | 2026-10-11T00:41:43.513008Z | yes |
| opencode/1.18.35 | mcp-stdio | 1 | emr_latest | 2026-10-11T00:39:18.759626Z | yes |

## Script printout

```
PASS     writer_of_old_record_is_codex: mem-1dc144c193a1 source_agent='codex'
PASS     writer_of_new_record_is_claude: mem-c42330528ad5 source_agent='claude'
PASS     supersede_link_is_correct: mem-c42330528ad5.supersedes='mem-1dc144c193a1'; mem-1dc144c193a1.superseded_by='mem-c42330528ad5'; old is superseded=True
PASS     newest_record_is_expected: emr_latest newest='mem-c42330528ad5', expected 'mem-c42330528ad5' (point in time)
PASS     digest_re_derives: sha256 over [id, created_at, status, lifecycle] of the 10 returned records = 3bed25d21d893e8a…; server said 3bed25d21d893e8a…
PASS     superseded_record_hidden_by_default: mem-1dc144c193a1 in default list: False; in include_superseded list: True
PASS     chain_verifies: history ok=True problems=[]; blocks ok=True problems=[]
PASS     old_receipt_re_derives: receipt verify ok=True problems=[]
PASS     history_seq_135_136_are_the_two_records: seq 135 -> 'mem-1dc144c193a1' ('create'); seq 136 -> 'mem-c42330528ad5' ('create')
NOT RUN  agent_digest_reproduces_from_seq_135_state: no agent-reported digest in the inputs file
PASS     call_log_chain_verifies: call log entries=54 head seq=54 problems=[]
```

## Raw command outputs

Every request the script made (method, path, query, HTTP status; no headers, no key):

```json
[
  {
    "method": "GET",
    "path": "/api/jarvis/replay/state",
    "query": {
      "limit": 1
    },
    "status": 200
  },
  {
    "method": "GET",
    "path": "/api/jarvis/memory/mem-1dc144c193a1",
    "query": null,
    "status": 200
  },
  {
    "method": "GET",
    "path": "/api/jarvis/memory/mem-c42330528ad5",
    "query": null,
    "status": 200
  },
  {
    "method": "GET",
    "path": "/api/jarvis/memory/mem-1dc144c193a1/history",
    "query": null,
    "status": 200
  },
  {
    "method": "GET",
    "path": "/api/jarvis/memory/mem-c42330528ad5/history",
    "query": null,
    "status": 200
  },
  {
    "method": "GET",
    "path": "/api/jarvis/replay/events",
    "query": {
      "from_seq": 135,
      "limit": 2
    },
    "status": 200
  },
  {
    "method": "GET",
    "path": "/api/jarvis/memory/history/verify",
    "query": null,
    "status": 200
  },
  {
    "method": "GET",
    "path": "/api/jarvis/blocks/verify",
    "query": null,
    "status": 200
  },
  {
    "method": "GET",
    "path": "/api/jarvis/blocks/head",
    "query": null,
    "status": 200
  },
  {
    "method": "GET",
    "path": "/api/jarvis/replay/receipts/eo:sha256:3f0d9fb25b8e82eaa4cfd6ee7e185cdf97c0a68dc3c8cef602041996ebf04a66/verify",
    "query": null,
    "status": 200
  },
  {
    "method": "POST",
    "path": "/api/jarvis/tools/emr_latest",
    "query": null,
    "status": 200
  },
  {
    "method": "POST",
    "path": "/api/jarvis/tools/emr_latest",
    "query": null,
    "status": 200
  },
  {
    "method": "POST",
    "path": "/api/jarvis/tools/emr_latest",
    "query": null,
    "status": 200
  },
  {
    "method": "GET",
    "path": "/api/jarvis/tools/calls",
    "query": {
      "limit": 200
    },
    "status": 200
  },
  {
    "method": "GET",
    "path": "/api/jarvis/tools/calls/verify",
    "query": null,
    "status": 200
  }
]
```

### `state` (HTTP 200)

```json
{
  "contract": "RC.Ledger.v1",
  "contract_version": 1,
  "tenant": "operator",
  "at_seq": 136,
  "history_seq": 136,
  "sealed_seq": 136,
  "sealed": true,
  "at_block_boundary": true,
  "block": {
    "height": 4,
    "first_seq": 135,
    "last_seq": 136,
    "block_hash": "36a22998920174d104bc500696ff6c21547cf9e50dad546f06ea661557336ce0",
    "signed": false,
    "signature_level": 0
  },
  "record_count": 61,
  "deleted_count": 15,
  "state_root": "78cb9ced870b551e4e00dd7e8258c68a5ef0dd659a2db99519e0bbb2b2b14537",
  "records": [
    {
      "id": "mem-07758cb93c03",
      "seq": 107,
      "version": 2,
      "row_hash": "832700e32aaa9ce8fdd663d2bdaae7b46bea111c9f44e93694b95bd13ffdb8c7",
      "record": {
        "id": "mem-07758cb93c03",
        "tags": [
          "personal-constitution-advisor",
          "ai-autonomous-construction",
          "specification-to-implementation",
          "human-cognitive-governance",
          "rapid-development"
        ],
        "type": "preference",
        "status": "archived",
        "content": "User immediately gave the Personal Constitutional Consistency Advisor specification to 'open code' (their AI systems) for autonomous implementation. This continues the pattern established with Worm Purpose Allocator: design specification, give to AI systems, receive complete implementation autonomously. The Personal Constitutional Consistency Advisor is a tool to help humans live according to their own principles by tracking values, monitoring decision consistency, and suggesting aligned actions. This applies their constitutional AI philosophy to human cognitive governance - giving humans the same constitutional clarity they're building for AI systems. The AI is now building it immediately after specification was provided.",
        "subject": "personal-constitution-advisor-autonomous-construction",
        "version": 2,
        "evidence": [
          {
            "ref": "user-statement-sha256:22c2d155d1b31c65b0a24d13",
            "kind": "user-request",
            "note": "i got open code building it now"
          }
        ],
        "confidence": 0.5,
        "created_at": "2026-09-20T13:39:38.091011+00:00",
        "session_id": "personal-constitution-advisor-ai-building",
        "supersedes": null,
        "tenant_key": "operator",
        "updated_at": "2026-10-05T21:46:45.092011+00:00",
        "source_agent": "mcp:devin",
        "content_sha256": "117757b1a028a245d159dbf41bd01c3d85774bde31df4ea85d80cbb4be1dbb6d"
      }
    }
  ],
  "next_after_id": "mem-07758cb93c03"
}
```

### `record_old` (HTTP 200)

```json
{
  "memory": {
    "id": "mem-1dc144c193a1",
    "content": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)",
    "created_at": "2026-10-10T20:39:50.728361+00:00",
    "updated_at": "2026-10-10T20:39:50.728361+00:00",
    "source_agent": "codex",
    "session_id": "01a12749-a6a0-7121-9925-fc420c624ceb",
    "type": "decision",
    "confidence": 0.5,
    "evidence": [
      {
        "kind": "deploy-report",
        "ref": "C:\\Users\\randj\\.claude\\projects\\ssh-103bcedb-aa49-452e-b45e-775fbd83e70e\\103bcedb-aa49-452e-b45e-775fbd83e70e.jsonl#timestamp=10/10/2026 19:20:38",
        "note": "Live deployment report: main 792e99b, schema 7 to 9, successful restore drill. Report text sha256 90465c91b552a70de6f7e5553232e9a6d5b7e71eacf73f02e895d8c8eabea554"
      },
      {
        "kind": "backup",
        "ref": "jarvis-20261010T191632Z",
        "note": "Final pre-deploy backup taken with app stopped; deploy report confirms offsite verification and retention copy."
      },
      {
        "kind": "backup",
        "ref": "jarvis-20261010T191930Z",
        "note": "Post-deploy schema 9 backup; deploy report confirms offsite verification and successful jarvisctl restore drill."
      }
    ],
    "supersedes": null,
    "status": "draft",
    "subject": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)",
    "tags": [],
    "version": 1,
    "content_sha256": "87863c892a63dfa2817f6be77aabb2afbd0c49c39962491af7bb24857901fc41"
  },
  "selection": {
    "memory_id": "mem-1dc144c193a1",
    "why_selected": "listed by recency (no filter); source_agent=codex; session_id=01a12749-a6a0-7121-9925-fc420c624ceb",
    "source_agent": "codex",
    "session_id": "01a12749-a6a0-7121-9925-fc420c624ceb",
    "created_at": "2026-10-10T20:39:50.728361+00:00",
    "type": "decision",
    "status": "draft",
    "confidence": 0.5,
    "supersedes": null,
    "evidence": [
      {
        "kind": "deploy-report",
        "ref": "C:\\Users\\randj\\.claude\\projects\\ssh-103bcedb-aa49-452e-b45e-775fbd83e70e\\103bcedb-aa49-452e-b45e-775fbd83e70e.jsonl#timestamp=10/10/2026 19:20:38",
        "note": "Live deployment report: main 792e99b, schema 7 to 9, successful restore drill. Report text sha256 90465c91b552a70de6f7e5553232e9a6d5b7e71eacf73f02e895d8c8eabea554"
      },
      {
        "kind": "backup",
        "ref": "jarvis-20261010T191632Z",
        "note": "Final pre-deploy backup taken with app stopped; deploy report confirms offsite verification and retention copy."
      },
      {
        "kind": "backup",
        "ref": "jarvis-20261010T191930Z",
        "note": "Post-deploy schema 9 backup; deploy report confirms offsite verification and successful jarvisctl restore drill."
      }
    ],
    "subject": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)"
  }
}
```

### `record_new` (HTTP 200)

```json
{
  "memory": {
    "id": "mem-c42330528ad5",
    "content": "Live ledger deployed from schema 7 to schema 9 on 2026-10-10 (main 792e99b, ~19:16-19:20Z), by Claude from a Jon-approved plan after a passing throwaway drill. Signing stayed shelved (JARVIS_SIGNATURES=warn, no trust roots, nothing signed); twin routes return 404. Corrects mem-1dc144c193a1, whose deploy-report evidence points at a path on the Windows PC that the ledger cannot read: the report is now a file on the box with a recorded hash.",
    "created_at": "2026-10-10T20:53:01.808201+00:00",
    "updated_at": "2026-10-10T20:53:01.808201+00:00",
    "source_agent": "claude",
    "session_id": "deploy-v8v9-2026-10-10",
    "type": "decision",
    "confidence": 0.5,
    "evidence": [
      {
        "kind": "deploy-report",
        "ref": "/home/jon/jarvis-ledger/drills/2026-10-10-v8v9/DEPLOY-REPORT.md",
        "note": "Plain-text deploy report on the box. sha256 ca74e35ae05ac7da2a1801ec61c0258f5f7bfc8f22ddbb38b774b765c40e6872"
      },
      {
        "kind": "backup",
        "ref": "jarvis-20261010T191632Z",
        "note": "Final pre-deploy backup taken with the app stopped; verified offsite; kept in ~/jarvis-ledger/keep-pre-v8v9/."
      },
      {
        "kind": "backup",
        "ref": "jarvis-20261010T191930Z",
        "note": "Post-deploy schema 9 backup; verified offsite; jarvisctl drill restored and verified it."
      }
    ],
    "supersedes": "mem-1dc144c193a1",
    "status": "draft",
    "subject": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)",
    "tags": [],
    "version": 1,
    "content_sha256": "13d1c47e02f1b7f1514ad23ac73d3cae4ebc2c38fdb38b5b06a489931d071b4f"
  },
  "selection": {
    "memory_id": "mem-c42330528ad5",
    "why_selected": "listed by recency (no filter); source_agent=claude; session_id=deploy-v8v9-2026-10-10",
    "source_agent": "claude",
    "session_id": "deploy-v8v9-2026-10-10",
    "created_at": "2026-10-10T20:53:01.808201+00:00",
    "type": "decision",
    "status": "draft",
    "confidence": 0.5,
    "supersedes": "mem-1dc144c193a1",
    "evidence": [
      {
        "kind": "deploy-report",
        "ref": "/home/jon/jarvis-ledger/drills/2026-10-10-v8v9/DEPLOY-REPORT.md",
        "note": "Plain-text deploy report on the box. sha256 ca74e35ae05ac7da2a1801ec61c0258f5f7bfc8f22ddbb38b774b765c40e6872"
      },
      {
        "kind": "backup",
        "ref": "jarvis-20261010T191632Z",
        "note": "Final pre-deploy backup taken with the app stopped; verified offsite; kept in ~/jarvis-ledger/keep-pre-v8v9/."
      },
      {
        "kind": "backup",
        "ref": "jarvis-20261010T191930Z",
        "note": "Post-deploy schema 9 backup; verified offsite; jarvisctl drill restored and verified it."
      }
    ],
    "subject": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)"
  }
}
```

### `history_old` (HTTP 200)

```json
{
  "memory_id": "mem-1dc144c193a1",
  "history": [
    {
      "history_id": 222,
      "seq": 135,
      "memory_id": "mem-1dc144c193a1",
      "version": 1,
      "op": "create",
      "actor": "operator",
      "changed_at": "2026-10-10T20:39:50.728836+00:00",
      "before": null,
      "after": {
        "id": "mem-1dc144c193a1",
        "tags": [],
        "type": "decision",
        "status": "draft",
        "content": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)",
        "subject": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)",
        "version": 1,
        "evidence": [
          {
            "ref": "C:\\Users\\randj\\.claude\\projects\\ssh-103bcedb-aa49-452e-b45e-775fbd83e70e\\103bcedb-aa49-452e-b45e-775fbd83e70e.jsonl#timestamp=10/10/2026 19:20:38",
            "kind": "deploy-report",
            "note": "Live deployment report: main 792e99b, schema 7 to 9, successful restore drill. Report text sha256 90465c91b552a70de6f7e5553232e9a6d5b7e71eacf73f02e895d8c8eabea554"
          },
          {
            "ref": "jarvis-20261010T191632Z",
            "kind": "backup",
            "note": "Final pre-deploy backup taken with app stopped; deploy report confirms offsite verification and retention copy."
          },
          {
            "ref": "jarvis-20261010T191930Z",
            "kind": "backup",
            "note": "Post-deploy schema 9 backup; deploy report confirms offsite verification and successful jarvisctl restore drill."
          }
        ],
        "confidence": 0.5,
        "created_at": "2026-10-10T20:39:50.728361+00:00",
        "session_id": "01a12749-a6a0-7121-9925-fc420c624ceb",
        "supersedes": null,
        "tenant_key": "operator",
        "updated_at": "2026-10-10T20:39:50.728361+00:00",
        "source_agent": "codex",
        "content_sha256": "87863c892a63dfa2817f6be77aabb2afbd0c49c39962491af7bb24857901fc41"
      },
      "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
      "row_hash": "1594680ea8448680772c9db9f0e7d7baaa7ee460506b452f20eda12c34499d8a"
    }
  ]
}
```

### `history_new` (HTTP 200)

```json
{
  "memory_id": "mem-c42330528ad5",
  "history": [
    {
      "history_id": 223,
      "seq": 136,
      "memory_id": "mem-c42330528ad5",
      "version": 1,
      "op": "create",
      "actor": "operator",
      "changed_at": "2026-10-10T20:53:01.808651+00:00",
      "before": null,
      "after": {
        "id": "mem-c42330528ad5",
        "tags": [],
        "type": "decision",
        "status": "draft",
        "content": "Live ledger deployed from schema 7 to schema 9 on 2026-10-10 (main 792e99b, ~19:16-19:20Z), by Claude from a Jon-approved plan after a passing throwaway drill. Signing stayed shelved (JARVIS_SIGNATURES=warn, no trust roots, nothing signed); twin routes return 404. Corrects mem-1dc144c193a1, whose deploy-report evidence points at a path on the Windows PC that the ledger cannot read: the report is now a file on the box with a recorded hash.",
        "subject": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)",
        "version": 1,
        "evidence": [
          {
            "ref": "/home/jon/jarvis-ledger/drills/2026-10-10-v8v9/DEPLOY-REPORT.md",
            "kind": "deploy-report",
            "note": "Plain-text deploy report on the box. sha256 ca74e35ae05ac7da2a1801ec61c0258f5f7bfc8f22ddbb38b774b765c40e6872"
          },
          {
            "ref": "jarvis-20261010T191632Z",
            "kind": "backup",
            "note": "Final pre-deploy backup taken with the app stopped; verified offsite; kept in ~/jarvis-ledger/keep-pre-v8v9/."
          },
          {
            "ref": "jarvis-20261010T191930Z",
            "kind": "backup",
            "note": "Post-deploy schema 9 backup; verified offsite; jarvisctl drill restored and verified it."
          }
        ],
        "confidence": 0.5,
        "created_at": "2026-10-10T20:53:01.808201+00:00",
        "session_id": "deploy-v8v9-2026-10-10",
        "supersedes": "mem-1dc144c193a1",
        "tenant_key": "operator",
        "updated_at": "2026-10-10T20:53:01.808201+00:00",
        "source_agent": "claude",
        "content_sha256": "13d1c47e02f1b7f1514ad23ac73d3cae4ebc2c38fdb38b5b06a489931d071b4f"
      },
      "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
      "row_hash": "1442376122d16a09a79fb825ed1d310e688e09129f311f46794fcb02ac3708aa"
    }
  ]
}
```

### `events_135_136` (HTTP 200)

```json
{
  "contract": "RC.Ledger.v1",
  "contract_version": 1,
  "tenant": "operator",
  "from_seq": 135,
  "to_seq": 136,
  "history_seq": 136,
  "events": [
    {
      "seq": 135,
      "memory_id": "mem-1dc144c193a1",
      "op": "create",
      "version": 1,
      "actor": "operator",
      "changed_at": "2026-10-10T20:39:50.728836+00:00",
      "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
      "row_hash": "1594680ea8448680772c9db9f0e7d7baaa7ee460506b452f20eda12c34499d8a",
      "before": null,
      "after": {
        "id": "mem-1dc144c193a1",
        "tags": [],
        "type": "decision",
        "status": "draft",
        "content": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)",
        "subject": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)",
        "version": 1,
        "evidence": [
          {
            "ref": "C:\\Users\\randj\\.claude\\projects\\ssh-103bcedb-aa49-452e-b45e-775fbd83e70e\\103bcedb-aa49-452e-b45e-775fbd83e70e.jsonl#timestamp=10/10/2026 19:20:38",
            "kind": "deploy-report",
            "note": "Live deployment report: main 792e99b, schema 7 to 9, successful restore drill. Report text sha256 90465c91b552a70de6f7e5553232e9a6d5b7e71eacf73f02e895d8c8eabea554"
          },
          {
            "ref": "jarvis-20261010T191632Z",
            "kind": "backup",
            "note": "Final pre-deploy backup taken with app stopped; deploy report confirms offsite verification and retention copy."
          },
          {
            "ref": "jarvis-20261010T191930Z",
            "kind": "backup",
            "note": "Post-deploy schema 9 backup; deploy report confirms offsite verification and successful jarvisctl restore drill."
          }
        ],
        "confidence": 0.5,
        "created_at": "2026-10-10T20:39:50.728361+00:00",
        "session_id": "01a12749-a6a0-7121-9925-fc420c624ceb",
        "supersedes": null,
        "tenant_key": "operator",
        "updated_at": "2026-10-10T20:39:50.728361+00:00",
        "source_agent": "codex",
        "content_sha256": "87863c892a63dfa2817f6be77aabb2afbd0c49c39962491af7bb24857901fc41"
      },
      "evidence": [
        {
          "kind": "deploy-report",
          "ref": "C:\\Users\\randj\\.claude\\projects\\ssh-103bcedb-aa49-452e-b45e-775fbd83e70e\\103bcedb-aa49-452e-b45e-775fbd83e70e.jsonl#timestamp=10/10/2026 19:20:38",
          "note": "Live deployment report: main 792e99b, schema 7 to 9, successful restore drill. Report text sha256 90465c91b552a70de6f7e5553232e9a6d5b7e71eacf73f02e895d8c8eabea554",
          "status": "not-checked"
        },
        {
          "kind": "backup",
          "ref": "jarvis-20261010T191632Z",
          "note": "Final pre-deploy backup taken with app stopped; deploy report confirms offsite verification and retention copy.",
          "status": "not-checked"
        },
        {
          "kind": "backup",
          "ref": "jarvis-20261010T191930Z",
          "note": "Post-deploy schema 9 backup; deploy report confirms offsite verification and successful jarvisctl restore drill.",
          "status": "not-checked"
        }
      ]
    },
    {
      "seq": 136,
      "memory_id": "mem-c42330528ad5",
      "op": "create",
      "version": 1,
      "actor": "operator",
      "changed_at": "2026-10-10T20:53:01.808651+00:00",
      "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
      "row_hash": "1442376122d16a09a79fb825ed1d310e688e09129f311f46794fcb02ac3708aa",
      "before": null,
      "after": {
        "id": "mem-c42330528ad5",
        "tags": [],
        "type": "decision",
        "status": "draft",
        "content": "Live ledger deployed from schema 7 to schema 9 on 2026-10-10 (main 792e99b, ~19:16-19:20Z), by Claude from a Jon-approved plan after a passing throwaway drill. Signing stayed shelved (JARVIS_SIGNATURES=warn, no trust roots, nothing signed); twin routes return 404. Corrects mem-1dc144c193a1, whose deploy-report evidence points at a path on the Windows PC that the ledger cannot read: the report is now a file on the box with a recorded hash.",
        "subject": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)",
        "version": 1,
        "evidence": [
          {
            "ref": "/home/jon/jarvis-ledger/drills/2026-10-10-v8v9/DEPLOY-REPORT.md",
            "kind": "deploy-report",
            "note": "Plain-text deploy report on the box. sha256 ca74e35ae05ac7da2a1801ec61c0258f5f7bfc8f22ddbb38b774b765c40e6872"
          },
          {
            "ref": "jarvis-20261010T191632Z",
            "kind": "backup",
            "note": "Final pre-deploy backup taken with the app stopped; verified offsite; kept in ~/jarvis-ledger/keep-pre-v8v9/."
          },
          {
            "ref": "jarvis-20261010T191930Z",
            "kind": "backup",
            "note": "Post-deploy schema 9 backup; verified offsite; jarvisctl drill restored and verified it."
          }
        ],
        "confidence": 0.5,
        "created_at": "2026-10-10T20:53:01.808201+00:00",
        "session_id": "deploy-v8v9-2026-10-10",
        "supersedes": "mem-1dc144c193a1",
        "tenant_key": "operator",
        "updated_at": "2026-10-10T20:53:01.808201+00:00",
        "source_agent": "claude",
        "content_sha256": "13d1c47e02f1b7f1514ad23ac73d3cae4ebc2c38fdb38b5b06a489931d071b4f"
      },
      "evidence": [
        {
          "kind": "deploy-report",
          "ref": "/home/jon/jarvis-ledger/drills/2026-10-10-v8v9/DEPLOY-REPORT.md",
          "note": "Plain-text deploy report on the box. sha256 ca74e35ae05ac7da2a1801ec61c0258f5f7bfc8f22ddbb38b774b765c40e6872",
          "status": "not-checked"
        },
        {
          "kind": "backup",
          "ref": "jarvis-20261010T191632Z",
          "note": "Final pre-deploy backup taken with the app stopped; verified offsite; kept in ~/jarvis-ledger/keep-pre-v8v9/.",
          "status": "not-checked"
        },
        {
          "kind": "backup",
          "ref": "jarvis-20261010T191930Z",
          "note": "Post-deploy schema 9 backup; verified offsite; jarvisctl drill restored and verified it.",
          "status": "not-checked"
        }
      ]
    }
  ],
  "next_from_seq": null
}
```

### `history_verify` (HTTP 200)

```json
{
  "ok": true,
  "problems": []
}
```

### `blocks_verify` (HTTP 200)

```json
{
  "ok": true,
  "problems": []
}
```

### `blocks_head` (HTTP 200)

```json
{
  "tip": {
    "height": 4,
    "first_seq": 135,
    "last_seq": 136,
    "entry_count": 2,
    "prev_block_hash": "006c730edcb73fa9053798a9322a7f54e8d88dc67c929376c2a5ac21bcec1f48",
    "entries_root": "446eb2dfae999249435a710094204b25038dd6f8bca8c747c1d7a0e11f96e1b6",
    "block_hash": "36a22998920174d104bc500696ff6c21547cf9e50dad546f06ea661557336ce0",
    "format": 1,
    "sealed_at": "2026-10-10T21:55:56.199952+00:00",
    "sealed_by": "operator"
  },
  "sealed_seq": 136,
  "history_seq": 136,
  "unsealed_entries": 0,
  "oldest_unsealed_at": null
}
```

### `receipt_verify` (HTTP 200)

```json
{
  "ok": true,
  "receipt_id": "eo:sha256:3f0d9fb25b8e82eaa4cfd6ee7e185cdf97c0a68dc3c8cef602041996ebf04a66",
  "problems": [],
  "receipt": {
    "contract": "RC.Ledger.v1",
    "contract_version": 1,
    "tenant": "operator",
    "at_seq": 132,
    "block_height": 1,
    "block_hash": "5df7daf4793ffa09c673dc5d67d0681d37f6b18eed2f577a714bc2e0f72addeb",
    "state_root": "3245d353b366fadd9346b3972d2d12c5bc2fbf248a74187a9a32093ec33087a3",
    "record_count": 57,
    "deleted_count": 15
  },
  "replayed": {
    "state_root": "3245d353b366fadd9346b3972d2d12c5bc2fbf248a74187a9a32093ec33087a3",
    "record_count": 57,
    "deleted_count": 15,
    "block": {
      "height": 1,
      "first_seq": 1,
      "last_seq": 132,
      "block_hash": "5df7daf4793ffa09c673dc5d67d0681d37f6b18eed2f577a714bc2e0f72addeb",
      "signed": false,
      "signature_level": 0
    }
  },
  "signatures": {
    "mode": "warn",
    "verified": false,
    "level": 0,
    "label": "L0 unsigned (not verified)",
    "block": {
      "height": 1,
      "level": 0,
      "attestation_seq": null,
      "label": "L0 unsigned"
    },
    "receipt": {
      "id": "eo:sha256:3f0d9fb25b8e82eaa4cfd6ee7e185cdf97c0a68dc3c8cef602041996ebf04a66",
      "level": 0,
      "attestation_seq": null,
      "label": "L0 unsigned"
    },
    "problems": [],
    "warnings": [
      "signatures not verified: no trust root is configured (JARVIS_TRUST_ROOTS_FILE), so nothing can be checked and nothing is called signed"
    ]
  }
}
```

### `latest_default` (HTTP 200)

```json
{
  "records": [
    {
      "id": "mem-c42330528ad5",
      "created_at": "2026-10-10T20:53:01.808201Z",
      "type": "decision",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "/home/jon/jarvis-ledger/drills/2026-10-10-v8v9/DEPLOY-REPORT.md",
          "jarvis-20261010T191632Z",
          "jarvis-20261010T191930Z"
        ]
      },
      "supersedes": "mem-1dc144c193a1",
      "superseded_by": null,
      "summary": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)"
    },
    {
      "id": "mem-a8474de660c1",
      "created_at": "2026-10-09T03:41:01.688088Z",
      "type": "fact",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "git log --format=%h %ad %s",
          "G:/nx-search/lib/vision.js",
          "npm test"
        ]
      },
      "supersedes": "mem-35fac5491bbc",
      "superseded_by": null,
      "summary": "nx-search"
    },
    {
      "id": "mem-7f3087cca34c",
      "created_at": "2026-10-06T07:58:16.787888Z",
      "type": "fact",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-deploy-check",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "eo:sha256:824c90ff516b7f4e5c70b2fab6084aef8fbcffd49cca57bbe2f31dbabc5f3c16"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "After the Evidence Objects deploy the live ledger database is at schema version 5."
    },
    {
      "id": "mem-be5bacb73988",
      "created_at": "2026-10-05T23:06:44.713688Z",
      "type": "decision",
      "status": "verified",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-triage",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "mem-436b8efad8cd",
          "mem-7e601d39a333",
          "mem-b696ffa95ae5",
          "mem-5e7676373052",
          "mem-1e1775e532f9",
          "mem-e49b834a5c7d",
          "mem-8b832bb4e9ca",
          "chat:ledger-triage-2026-10-05"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "user-stated-values"
    },
    {
      "id": "mem-30a74f317181",
      "created_at": "2026-10-05T21:46:24.418432Z",
      "type": "decision",
      "status": "verified",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-triage",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "chat:ledger-triage-2026-10-05",
          "mem-be11d862660d"
        ]
      },
      "supersedes": "mem-be11d862660d",
      "superseded_by": null,
      "summary": "jarvis-ledger-deployment"
    },
    {
      "id": "mem-da3ddefda22a",
      "created_at": "2026-10-05T11:43:46.613905Z",
      "type": "fact",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-setup",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "port-8001:mem-79f9aa7e7e44"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "llm-gateway deployment"
    },
    {
      "id": "mem-3566ccf6a19d",
      "created_at": "2026-09-22T07:45:35.559005Z",
      "type": "architecture",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:740320a83a329dc921383cdc"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "cognitive-architecture-hyper-systemizer"
    },
    {
      "id": "mem-ace10a907b4e",
      "created_at": "2026-09-20T13:44:31.801407Z",
      "type": "preference",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:3a51d477cc5399565e993eb9"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "production-grade-build-plan"
    },
    {
      "id": "mem-2fb34d106dfe",
      "created_at": "2026-09-20T12:43:40.326058Z",
      "type": "preference",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:5960ef78e1e269f42915e06f"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "github-repository-publication"
    },
    {
      "id": "mem-e679ed8a4de1",
      "created_at": "2026-09-20T12:22:55.937188Z",
      "type": "preference",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:4e66e296f6f5cc8e827144d4"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "ai-autonomous-implementation-achievement"
    }
  ],
  "next_cursor": "eyJjIjoiMjAyNi0wOS0yMFQxMjoyMjo1NS45MzcxODhaIiwiaSI6Im1lbS1lNjc5ZWQ4YTRkZTEiLCJzIjoiYTU3MGJmNDNhNDFiM2EzMGZkZTMyMGU2In0.QHEAOowiqFiZYdq7Sv1sjm2ogKCuqDodcYDjpeukyRY",
  "tenant": "operator",
  "ledger_head": "block:36a22998920174d104bc500696ff6c21547cf9e50dad546f06ea661557336ce0",
  "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
  "provenance": "ledger"
}
```

### `latest_default_12` (HTTP 200)

```json
{
  "records": [
    {
      "id": "mem-c42330528ad5",
      "created_at": "2026-10-10T20:53:01.808201Z",
      "type": "decision",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "/home/jon/jarvis-ledger/drills/2026-10-10-v8v9/DEPLOY-REPORT.md",
          "jarvis-20261010T191632Z",
          "jarvis-20261010T191930Z"
        ]
      },
      "supersedes": "mem-1dc144c193a1",
      "superseded_by": null,
      "summary": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)"
    },
    {
      "id": "mem-a8474de660c1",
      "created_at": "2026-10-09T03:41:01.688088Z",
      "type": "fact",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "git log --format=%h %ad %s",
          "G:/nx-search/lib/vision.js",
          "npm test"
        ]
      },
      "supersedes": "mem-35fac5491bbc",
      "superseded_by": null,
      "summary": "nx-search"
    },
    {
      "id": "mem-7f3087cca34c",
      "created_at": "2026-10-06T07:58:16.787888Z",
      "type": "fact",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-deploy-check",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "eo:sha256:824c90ff516b7f4e5c70b2fab6084aef8fbcffd49cca57bbe2f31dbabc5f3c16"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "After the Evidence Objects deploy the live ledger database is at schema version 5."
    },
    {
      "id": "mem-be5bacb73988",
      "created_at": "2026-10-05T23:06:44.713688Z",
      "type": "decision",
      "status": "verified",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-triage",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "mem-436b8efad8cd",
          "mem-7e601d39a333",
          "mem-b696ffa95ae5",
          "mem-5e7676373052",
          "mem-1e1775e532f9",
          "mem-e49b834a5c7d",
          "mem-8b832bb4e9ca",
          "chat:ledger-triage-2026-10-05"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "user-stated-values"
    },
    {
      "id": "mem-30a74f317181",
      "created_at": "2026-10-05T21:46:24.418432Z",
      "type": "decision",
      "status": "verified",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-triage",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "chat:ledger-triage-2026-10-05",
          "mem-be11d862660d"
        ]
      },
      "supersedes": "mem-be11d862660d",
      "superseded_by": null,
      "summary": "jarvis-ledger-deployment"
    },
    {
      "id": "mem-da3ddefda22a",
      "created_at": "2026-10-05T11:43:46.613905Z",
      "type": "fact",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-setup",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "port-8001:mem-79f9aa7e7e44"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "llm-gateway deployment"
    },
    {
      "id": "mem-3566ccf6a19d",
      "created_at": "2026-09-22T07:45:35.559005Z",
      "type": "architecture",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:740320a83a329dc921383cdc"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "cognitive-architecture-hyper-systemizer"
    },
    {
      "id": "mem-ace10a907b4e",
      "created_at": "2026-09-20T13:44:31.801407Z",
      "type": "preference",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:3a51d477cc5399565e993eb9"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "production-grade-build-plan"
    },
    {
      "id": "mem-2fb34d106dfe",
      "created_at": "2026-09-20T12:43:40.326058Z",
      "type": "preference",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:5960ef78e1e269f42915e06f"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "github-repository-publication"
    },
    {
      "id": "mem-e679ed8a4de1",
      "created_at": "2026-09-20T12:22:55.937188Z",
      "type": "preference",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:4e66e296f6f5cc8e827144d4"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "ai-autonomous-implementation-achievement"
    },
    {
      "id": "mem-8a349810e396",
      "created_at": "2026-09-20T11:53:05.069871Z",
      "type": "preference",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:77e8dd27b2648b980e818d33"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "hardware-configuration"
    },
    {
      "id": "mem-da2b06ee3a22",
      "created_at": "2026-09-20T09:51:46.986946Z",
      "type": "preference",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:325515b67d289d920a96854e"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "ai-autonomous-construction"
    }
  ],
  "next_cursor": "eyJjIjoiMjAyNi0wOS0yMFQwOTo1MTo0Ni45ODY5NDZaIiwiaSI6Im1lbS1kYTJiMDZlZTNhMjIiLCJzIjoiYTU3MGJmNDNhNDFiM2EzMGZkZTMyMGU2In0.nD697tWvtORpaeJvFy5b1LUaT8WJOLM7Y7TE3hyRCAs",
  "tenant": "operator",
  "ledger_head": "block:36a22998920174d104bc500696ff6c21547cf9e50dad546f06ea661557336ce0",
  "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
  "provenance": "ledger"
}
```

### `latest_with_superseded` (HTTP 200)

```json
{
  "records": [
    {
      "id": "mem-c42330528ad5",
      "created_at": "2026-10-10T20:53:01.808201Z",
      "type": "decision",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "/home/jon/jarvis-ledger/drills/2026-10-10-v8v9/DEPLOY-REPORT.md",
          "jarvis-20261010T191632Z",
          "jarvis-20261010T191930Z"
        ]
      },
      "supersedes": "mem-1dc144c193a1",
      "superseded_by": null,
      "summary": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)"
    },
    {
      "id": "mem-1dc144c193a1",
      "created_at": "2026-10-10T20:39:50.728361Z",
      "type": "decision",
      "status": "draft",
      "lifecycle": "superseded",
      "provenance": {
        "source_agent": "codex",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "C:\\Users\\randj\\.claude\\projects\\ssh-103bcedb-aa49-452e-b45e-775fbd83e70e\\103bcedb-aa49-452e-b45e-775fbd83e70e.jsonl#timestamp=10/10/2026 19:20:38",
          "jarvis-20261010T191632Z",
          "jarvis-20261010T191930Z"
        ]
      },
      "supersedes": null,
      "superseded_by": "mem-c42330528ad5",
      "summary": "Schema v9 deployed to live on 2026-10-10 (main 792e99b)"
    },
    {
      "id": "mem-a8474de660c1",
      "created_at": "2026-10-09T03:41:01.688088Z",
      "type": "fact",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "git log --format=%h %ad %s",
          "G:/nx-search/lib/vision.js",
          "npm test"
        ]
      },
      "supersedes": "mem-35fac5491bbc",
      "superseded_by": null,
      "summary": "nx-search"
    },
    {
      "id": "mem-35fac5491bbc",
      "created_at": "2026-10-09T00:59:16.306721Z",
      "type": "fact",
      "status": "draft",
      "lifecycle": "superseded",
      "provenance": {
        "source_agent": "devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "G:/nx-search",
          "G:/nx-search/lib/vision.js",
          "npm test",
          "G:/persistence-memory/UNIFIED_MEMORY_SYSTEM.md"
        ]
      },
      "supersedes": null,
      "superseded_by": "mem-a8474de660c1",
      "summary": "nx-search"
    },
    {
      "id": "mem-7f3087cca34c",
      "created_at": "2026-10-06T07:58:16.787888Z",
      "type": "fact",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-deploy-check",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "eo:sha256:824c90ff516b7f4e5c70b2fab6084aef8fbcffd49cca57bbe2f31dbabc5f3c16"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "After the Evidence Objects deploy the live ledger database is at schema version 5."
    },
    {
      "id": "mem-be5bacb73988",
      "created_at": "2026-10-05T23:06:44.713688Z",
      "type": "decision",
      "status": "verified",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-triage",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "mem-436b8efad8cd",
          "mem-7e601d39a333",
          "mem-b696ffa95ae5",
          "mem-5e7676373052",
          "mem-1e1775e532f9",
          "mem-e49b834a5c7d",
          "mem-8b832bb4e9ca",
          "chat:ledger-triage-2026-10-05"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "user-stated-values"
    },
    {
      "id": "mem-30a74f317181",
      "created_at": "2026-10-05T21:46:24.418432Z",
      "type": "decision",
      "status": "verified",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-triage",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "chat:ledger-triage-2026-10-05",
          "mem-be11d862660d"
        ]
      },
      "supersedes": "mem-be11d862660d",
      "superseded_by": null,
      "summary": "jarvis-ledger-deployment"
    },
    {
      "id": "mem-da3ddefda22a",
      "created_at": "2026-10-05T11:43:46.613905Z",
      "type": "fact",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "claude-code-setup",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "port-8001:mem-79f9aa7e7e44"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "llm-gateway deployment"
    },
    {
      "id": "mem-3566ccf6a19d",
      "created_at": "2026-09-22T07:45:35.559005Z",
      "type": "architecture",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:740320a83a329dc921383cdc"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "cognitive-architecture-hyper-systemizer"
    },
    {
      "id": "mem-ace10a907b4e",
      "created_at": "2026-09-20T13:44:31.801407Z",
      "type": "preference",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:3a51d477cc5399565e993eb9"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "production-grade-build-plan"
    },
    {
      "id": "mem-2fb34d106dfe",
      "created_at": "2026-09-20T12:43:40.326058Z",
      "type": "preference",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:5960ef78e1e269f42915e06f"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "github-repository-publication"
    },
    {
      "id": "mem-e679ed8a4de1",
      "created_at": "2026-09-20T12:22:55.937188Z",
      "type": "preference",
      "status": "draft",
      "lifecycle": "active",
      "provenance": {
        "source_agent": "mcp:devin",
        "actor": null,
        "method": null,
        "evidence_refs": [
          "user-statement-sha256:4e66e296f6f5cc8e827144d4"
        ]
      },
      "supersedes": null,
      "superseded_by": null,
      "summary": "ai-autonomous-implementation-achievement"
    }
  ],
  "next_cursor": "eyJjIjoiMjAyNi0wOS0yMFQxMjoyMjo1NS45MzcxODhaIiwiaSI6Im1lbS1lNjc5ZWQ4YTRkZTEiLCJzIjoiYWZhYWI3YzMwMWE4ZjFkZmNhNjJmNDBkIn0.RdWTYEeHD8nzIf_wpuRdttVQG0KYu-TYvg5zuBIagkU",
  "tenant": "operator",
  "ledger_head": "block:36a22998920174d104bc500696ff6c21547cf9e50dad546f06ea661557336ce0",
  "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
  "provenance": "ledger"
}
```

### `calls_all` (HTTP 200)

```json
{
  "entries": [
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 21,
      "entry_hash": "7d06a2da15dfb2da9dc8c42f0360e6f17d83854d281b648ae4a9f174dc61720f",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "ad8dd56ad73404eeee58ebf454266838b843ed18af983328f2b292acd91373b6",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 54,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T02:04:37.025948Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 22,
      "entry_hash": "ad8dd56ad73404eeee58ebf454266838b843ed18af983328f2b292acd91373b6",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "a5f0c2e2e2daa9d281db3e2125582a4ea31a97c2801ed0f8e8eee6924bb8acc5",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 53,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T02:04:36.959700Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 6,
      "entry_hash": "a5f0c2e2e2daa9d281db3e2125582a4ea31a97c2801ed0f8e8eee6924bb8acc5",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "cf90592f737a2ea749d76473e58bd27bd828d68ea76ba795af8953a08f13becd",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 52,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T02:04:36.871760Z"
    },
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 23,
      "entry_hash": "cf90592f737a2ea749d76473e58bd27bd828d68ea76ba795af8953a08f13becd",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "ede63174123aef63ec8db0719f7292e36c8ef2729ae18645750748e0894de18a",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 51,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T02:03:47.061245Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 20,
      "entry_hash": "ede63174123aef63ec8db0719f7292e36c8ef2729ae18645750748e0894de18a",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "65668c343d5447f8f150cb125d43f8c6a85a5d0176fda16d64c9da9a0208de7e",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 50,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T02:03:46.988998Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 5,
      "entry_hash": "65668c343d5447f8f150cb125d43f8c6a85a5d0176fda16d64c9da9a0208de7e",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "2a52317a2301db600771c56a961550f570116228e16828a57c7fc610badf345a",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 49,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T02:03:46.917668Z"
    },
    {
      "args_sha256": "80eb6a3f04273ff5137d233576d17bff1e9064ca18eda08b4dafc49b08c45ef6",
      "client_name": "curl",
      "client_self_reported": true,
      "client_version": "8.5.0",
      "duration_ms": 5,
      "entry_hash": "2a52317a2301db600771c56a961550f570116228e16828a57c7fc610badf345a",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "e225c8bb6e6b1b142cbdc97cfc3c63c97bdb76e6a9af8de9010dcb0e24c06ab7",
      "result_digest": null,
      "route": "/api/jarvis/blocks/seal",
      "seq": 48,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "POST /api/jarvis/blocks/seal",
      "transport": "http-api",
      "ts": "2026-10-11T01:55:56.193101Z"
    },
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 22,
      "entry_hash": "e225c8bb6e6b1b142cbdc97cfc3c63c97bdb76e6a9af8de9010dcb0e24c06ab7",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "5c61e0fa1020de20a7255a28e45040530e7228729f11de7ecfd9489c327ca1c5",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 47,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:54:42.515401Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 23,
      "entry_hash": "5c61e0fa1020de20a7255a28e45040530e7228729f11de7ecfd9489c327ca1c5",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "39e40fa965fa82798bd33367a8a8ce1a5ebce16b94f58bd2aaa20d84cb6b1070",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 46,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:54:42.439570Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 7,
      "entry_hash": "39e40fa965fa82798bd33367a8a8ce1a5ebce16b94f58bd2aaa20d84cb6b1070",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "2315361fb156aaceef354138f7fb1d16a34ae34d8d28256d8282418e9036247b",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 45,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:54:42.340506Z"
    },
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 21,
      "entry_hash": "2315361fb156aaceef354138f7fb1d16a34ae34d8d28256d8282418e9036247b",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "58f6a66b484f3e13b25a709236b2fb76255c0c401bf244f85eecd3d7bb2e86a4",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 44,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:53:52.976377Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 21,
      "entry_hash": "58f6a66b484f3e13b25a709236b2fb76255c0c401bf244f85eecd3d7bb2e86a4",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "ac201ed22c34f0f50f50378bd6fc1ad0d6c3541c3028f7b5d91dd19636121e01",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 43,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:53:52.919987Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 6,
      "entry_hash": "ac201ed22c34f0f50f50378bd6fc1ad0d6c3541c3028f7b5d91dd19636121e01",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "87ed0de5bdbe5121dd680c982643de15bd78c0b4d185a9d88e375017f3975cbf",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 42,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:53:52.842308Z"
    },
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 22,
      "entry_hash": "87ed0de5bdbe5121dd680c982643de15bd78c0b4d185a9d88e375017f3975cbf",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "fc4a22af40a7c4c24cd2fc0a83c882cd1cd6a95866d04dff1334c194965427b7",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 41,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:13:14.456989Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 20,
      "entry_hash": "fc4a22af40a7c4c24cd2fc0a83c882cd1cd6a95866d04dff1334c194965427b7",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "71ae568f51fae2c8a82676b794398c7cf481ec51e3afe6a210fa2c5fcab592ed",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 40,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:13:14.386412Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 7,
      "entry_hash": "71ae568f51fae2c8a82676b794398c7cf481ec51e3afe6a210fa2c5fcab592ed",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "1c17f10e00cf500e6e1e3f59303f6b2c2de1c6e265ca3c6874fd4452c4b309a1",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 39,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:13:14.318062Z"
    },
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 21,
      "entry_hash": "1c17f10e00cf500e6e1e3f59303f6b2c2de1c6e265ca3c6874fd4452c4b309a1",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "9622c94e24b0126e2a850b0b94467bd2ea9ccf98a7fc6384d008e602ac5a7d2c",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 38,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:04:13.894385Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 9,
      "entry_hash": "9622c94e24b0126e2a850b0b94467bd2ea9ccf98a7fc6384d008e602ac5a7d2c",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "4e9ebd62899ae3ffe25933fcb8a4d37ff16a5497dcc4473dd46a5579c51554ec",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 37,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:04:13.835431Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 6,
      "entry_hash": "4e9ebd62899ae3ffe25933fcb8a4d37ff16a5497dcc4473dd46a5579c51554ec",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "c189857a667eb1e7661b6d59353be9c934be3b922b445f2a966ed46cbc962a42",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 36,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:04:13.792962Z"
    },
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 19,
      "entry_hash": "c189857a667eb1e7661b6d59353be9c934be3b922b445f2a966ed46cbc962a42",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "23865e297d3dee96b17b05f5cdbabbbf98a4468ef52f5f6f017c4883a428a73d",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 35,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T01:00:06.137357Z"
    }
  ],
  "pages": 1,
  "truncated": false,
  "head_seq": 54,
  "head_mismatch": false,
  "entries_omitted_from_this_listing": 34
}
```

### `calls_verify` (HTTP 200)

```json
{
  "ok": true,
  "entries": 54,
  "files": [
    "calls-20261011.jsonl"
  ],
  "problems": [],
  "head": {
    "seq": 54,
    "entry_hash": "7d06a2da15dfb2da9dc8c42f0360e6f17d83854d281b648ae4a9f174dc61720f"
  },
  "anchor": null,
  "degraded": null,
  "gaps_recorded": 0
}
```
