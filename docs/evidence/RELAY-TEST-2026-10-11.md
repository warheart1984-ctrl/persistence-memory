# Six-agent relay test on the call-log build, 2026-10-11

Generated 2026-10-11T00:44:20Z by `scripts/relay_evidence.py` against `http://127.0.0.1:8011` (read-only (GET reads and read-only tool POSTs only)).

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
| **PASS** | `call_log_chain_verifies` | call log entries=21 head seq=21 problems=[] |

All checks passed: **True**

## Known gaps and deviations

- The call log has existed only since the deploy (about 00:18Z on 2026-10-11); nothing before that was witnessed by the server. The earlier relay run (2026-10-10) can only be compared through the agents' own reports.
- Client names are self-reported. A name in the log shows what the client called itself, not who was behind it. 'rmcp/3.1.0' (a Rust MCP client library name) made emr_latest and fetch calls over MCP stdio; it is one of Devin or Kilo, but the log cannot say which, and this report does not guess.
- Cursor's emr_latest call (seq 13) and the 'rmcp/3.1.0' client's emr_latest call (seq 14) were not made with no arguments: the argument hash in the log equals the hash of exactly {"limit": 5}, so both asked for five records and correctly got a different digest (018d59bdeaaeb4976e97c73051fc5c1dffefa9c284df380f0b2ae4e59e0ee93a), the same for both. The OpenCode (seq 8), Codex (seq 10 and 11) and Claude (seq 1) calls used no arguments and returned 3bed25d2.... The log keeps only a hash of the arguments, never the arguments themselves.
- Neither Devin nor Kilo appears under its own name in the server log as of this read. Either one of them is the 'rmcp' client and the other has not called emr_latest through the MCP bridge yet (or its proxy has not been updated), or neither is. Calls recorded as 'Python-urllib' are plain HTTP, not the MCP bridge; some are this report's own earlier dry run, the rest cannot be attributed.
- 'Newest record is mem-c42330528ad5' is a point-in-time check; the report records the history seq and the call-log head it was read at.
- The self-heal restart that happened during the previous (v8/v9) deploy did not recur in this deploy: the pause flag was held until the new app was healthy, and the alerts log has no entry since the deploy started.
- The call log's head hash printed in this report should be copied off the box, and into the next ledger record that is written (anyone who can write the log directory can rebuild a whole chain; trailing entries can be truncated without breaking it).

## Agent results: reported by agent, NOT verified by the script

These are what each agent said it saw, as pasted by the operator. The script did not run, observe or confirm any of them.

| Agent | Status of this entry | Newest id reported | Digest reported | Notes |
|---|---|---|---|---|
| Devin | no output pasted for this run | `-` | `-` | The operator said every agent ran the MCP call. Nothing from Devin was pasted, so only the server log speaks for it; see the call-log section. |
| OpenCode | no output pasted for this run | `-` | `-` | As above. Its checkout on the PC was updated and its MCP connection restarted before this run (per the operator). |
| Codex | no output pasted for this run | `-` | `-` | As above. |
| Cursor | no output pasted for this run | `-` | `-` | As above. (No Cursor output was supplied in the earlier run either.) |
| Kilo | no output pasted for this run | `-` | `-` | Kilo's ledger connection was added on 2026-10-10 (config copied from OpenCode's entry); this is its first relay run. |
| Claude | own call | `mem-c42330528ad5` | `-` | Claude has no MCP server configured on the PC, so its call is a curl from the Mint box with a self-reported client header (claude-code/deploy-check), made at the deploy check. |

## Server-witnessed calls to emr_latest (client names are self-reported)

Call log chain verifies: **True** (21 entries in 1 file(s)). **Head: seq 21, hash `a5ed81f7fef025e8827912ff6548f926cc3d876ab990e6a3abd7cd825c2e2475`**. Record this off the box, and in the next ledger record that is written.

| Agent | Witnessed emr_latest calls | Latest: seq, time, transport | Client as it reported itself | result_digest | Called with no arguments | Outcome |
|---|---|---|---|---|---|---|
| Devin | 0 | - | - | - | - | no emr_latest call from a client whose name contains any of 'devin' is in the part of the server's log that was read (it may have called under another name, before the log existed, or not at all: see the list of every client name the log saw) |
| OpenCode | 1 | 8, 2026-10-11T00:39:18.759626Z, mcp-stdio | opencode/1.18.35 | `3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae` | yes | ok |
| Codex | 2 | 11, 2026-10-11T00:40:19.365844Z, mcp-stdio | codex-mcp-client/0.162.0-alpha.17.2 | `3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae` | yes | ok |
| Cursor | 1 | 13, 2026-10-11T00:41:43.513008Z, mcp-stdio | cursor-vscode/1.0.0 | `018d59bdeaaeb4976e97c73051fc5c1dffefa9c284df380f0b2ae4e59e0ee93a` | no | ok |
| Kilo | 0 | - | - | - | - | no emr_latest call from a client whose name contains any of 'kilo' is in the part of the server's log that was read (it may have called under another name, before the log existed, or not at all: see the list of every client name the log saw) |
| Claude | 1 | 1, 2026-10-11T00:18:27.357524Z, http-tool | claude-code/deploy-check | `3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae` | yes | ok |

Every client name the log saw (coverage: complete: all 21 log entries (1 page(s)) were read; self-reported, so a name is a claim and not an identity). The last column says whether it was counted for one of the agents above. Rows named `relay-evidence` are this report's own read-only calls:

| Client name / version | Transport | Calls | Tools | Last seen | Matched an agent above |
|---|---|---|---|---|---|
| Python-urllib/3.12 | http-tool | 6 | emr_fetch, emr_latest | 2026-10-11T00:29:58.579976Z | **no** |
| relay-evidence/1 | http-tool | 6 | emr_latest | 2026-10-11T00:44:19.946981Z | **no** |
| codex-mcp-client/0.162.0-alpha.17.2 | mcp-stdio | 4 | emr_latest, emr_recall | 2026-10-11T00:40:19.532772Z | yes |
| rmcp/3.1.0 | mcp-stdio | 2 | emr_latest, fetch | 2026-10-11T00:42:12.685688Z | **no** |
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
PASS     call_log_chain_verifies: call log entries=21 head seq=21 problems=[]
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
      "tool": "emr_latest",
      "limit": 200
    },
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

### `calls_emr_latest` (HTTP 200)

```json
{
  "entries": [
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 21,
      "entry_hash": "a5ed81f7fef025e8827912ff6548f926cc3d876ab990e6a3abd7cd825c2e2475",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "edd32da9ada90b0111739ac2b3758737ee44402ae2937a10260a50098fcc3966",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 21,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:44:19.946981Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 21,
      "entry_hash": "edd32da9ada90b0111739ac2b3758737ee44402ae2937a10260a50098fcc3966",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "63d4720bd7526c503efaaa7c76c4b77199235ffb94ff6acf582891cd5d446232",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 20,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:44:19.858285Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 8,
      "entry_hash": "63d4720bd7526c503efaaa7c76c4b77199235ffb94ff6acf582891cd5d446232",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "37bc97d38b863395788b08e6c31a76ba0033f9e79a83319f2ba8553c4081a950",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 19,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:44:19.792570Z"
    },
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 21,
      "entry_hash": "37bc97d38b863395788b08e6c31a76ba0033f9e79a83319f2ba8553c4081a950",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "e3a61dc2ce7d6145c4fc2ca939b30dd9bdd1bdcda48223b66d40949e2eadb97a",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 18,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:43:44.018904Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 21,
      "entry_hash": "e3a61dc2ce7d6145c4fc2ca939b30dd9bdd1bdcda48223b66d40949e2eadb97a",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "30d1927993f63aca24997bbc44bb6e627e5ef2882301deb4a963c2062cde1fd7",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 17,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:43:43.952455Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 5,
      "entry_hash": "30d1927993f63aca24997bbc44bb6e627e5ef2882301deb4a963c2062cde1fd7",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "4647d36502f2c271342f9cee0b29ae218752e6fdeb33e5248d57a5e94de67b6e",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 16,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:43:43.883679Z"
    },
    {
      "args_sha256": "a03c8657a575356de5937110fc9038dafa42d659089628586aa8f8d73924cd16",
      "client_name": "rmcp",
      "client_self_reported": true,
      "client_version": "3.1.0",
      "duration_ms": 10,
      "entry_hash": "0a17e2afe18403d07b3e9f2e514448524e93bc39f66b432b53048c1f23ce875e",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "fbef467f1d4afb8488ef5213acbed7da859e667dd59f0504a3f3c5173a3aff9e",
      "result_digest": "018d59bdeaaeb4976e97c73051fc5c1dffefa9c284df380f0b2ae4e59e0ee93a",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 14,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:42:10.183500Z"
    },
    {
      "args_sha256": "a03c8657a575356de5937110fc9038dafa42d659089628586aa8f8d73924cd16",
      "client_name": "cursor-vscode",
      "client_self_reported": true,
      "client_version": "1.0.0",
      "duration_ms": 8,
      "entry_hash": "fbef467f1d4afb8488ef5213acbed7da859e667dd59f0504a3f3c5173a3aff9e",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "8217a58865b2ebf274ccf7a7371b6b4feab3fce07e54838f011a504679702864",
      "result_digest": "018d59bdeaaeb4976e97c73051fc5c1dffefa9c284df380f0b2ae4e59e0ee93a",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 13,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:41:43.513008Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "codex-mcp-client",
      "client_self_reported": true,
      "client_version": "0.162.0-alpha.17.2",
      "duration_ms": 21,
      "entry_hash": "afc81936f39ec80a50a864890f1759699defe69dea37800eac7a8cc587b0d2ab",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "6a6f126f382bf59cea72a4f65ded5a94de2ec0caa936f272ecbc6c2581901180",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 11,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:40:19.365844Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "codex-mcp-client",
      "client_self_reported": true,
      "client_version": "0.162.0-alpha.17.2",
      "duration_ms": 6,
      "entry_hash": "6a6f126f382bf59cea72a4f65ded5a94de2ec0caa936f272ecbc6c2581901180",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "fbcc90e6eed4f321725aae6c317feb77d068c431da4473265865e6b8b8f26562",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 10,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:39:49.210761Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "opencode",
      "client_self_reported": true,
      "client_version": "1.18.35",
      "duration_ms": 21,
      "entry_hash": "f1e082951bfa483f1616e3ba226788a969eedfd912c76e46bf9b1dc1ff5f1f64",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "492a0c2001837a1f6a83071c352c9b3cd389bcfae26ae4dde5fab2fde6b0a383",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 8,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:39:18.759626Z"
    },
    {
      "args_sha256": "ca502dec04523cdc33afece69a9b600d5b9bd022d453791cc693b6b372f808ad",
      "client_name": "Python-urllib",
      "client_self_reported": true,
      "client_version": "3.12",
      "duration_ms": 11,
      "entry_hash": "83fb8dc402b8bee7c3a162d9b22265b59e3223f8357a8f0add43974da3178a39",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "7ba521e3635a60e5bc9f7843c9f0c65534fae6ab34e6c6576078fdd4d06c7e47",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 6,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:29:14.026327Z"
    },
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "Python-urllib",
      "client_self_reported": true,
      "client_version": "3.12",
      "duration_ms": 21,
      "entry_hash": "878da51e7b1700dbf3f9c5a4660e0cc729d5c9671afd019ab6ee0e97348600ae",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "65c709024f2130a6ae4f00602de1f65eb25ffff57d54b7c5c951816ddc9f83b4",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 4,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:26:21.489415Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "Python-urllib",
      "client_self_reported": true,
      "client_version": "3.12",
      "duration_ms": 22,
      "entry_hash": "65c709024f2130a6ae4f00602de1f65eb25ffff57d54b7c5c951816ddc9f83b4",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "91ae8beb8d098d05c6823ee605af0a37827db6a7f6c0a0ea2f69e38b40668414",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 3,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:26:21.422657Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "Python-urllib",
      "client_self_reported": true,
      "client_version": "3.12",
      "duration_ms": 6,
      "entry_hash": "91ae8beb8d098d05c6823ee605af0a37827db6a7f6c0a0ea2f69e38b40668414",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "770a1d60362580269bee6de36774f60c10429270ed9f3d04d5beb69e1b56a58a",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 2,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:26:21.343969Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "claude-code",
      "client_self_reported": true,
      "client_version": "deploy-check",
      "duration_ms": 9,
      "entry_hash": "770a1d60362580269bee6de36774f60c10429270ed9f3d04d5beb69e1b56a58a",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "0000000000000000000000000000000000000000000000000000000000000000",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 1,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:18:27.357524Z"
    }
  ],
  "pages": 1,
  "truncated": false
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
      "entry_hash": "a5ed81f7fef025e8827912ff6548f926cc3d876ab990e6a3abd7cd825c2e2475",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "edd32da9ada90b0111739ac2b3758737ee44402ae2937a10260a50098fcc3966",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 21,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:44:19.946981Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 21,
      "entry_hash": "edd32da9ada90b0111739ac2b3758737ee44402ae2937a10260a50098fcc3966",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "63d4720bd7526c503efaaa7c76c4b77199235ffb94ff6acf582891cd5d446232",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 20,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:44:19.858285Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 8,
      "entry_hash": "63d4720bd7526c503efaaa7c76c4b77199235ffb94ff6acf582891cd5d446232",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "37bc97d38b863395788b08e6c31a76ba0033f9e79a83319f2ba8553c4081a950",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 19,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:44:19.792570Z"
    },
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 21,
      "entry_hash": "37bc97d38b863395788b08e6c31a76ba0033f9e79a83319f2ba8553c4081a950",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "e3a61dc2ce7d6145c4fc2ca939b30dd9bdd1bdcda48223b66d40949e2eadb97a",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 18,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:43:44.018904Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 21,
      "entry_hash": "e3a61dc2ce7d6145c4fc2ca939b30dd9bdd1bdcda48223b66d40949e2eadb97a",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "30d1927993f63aca24997bbc44bb6e627e5ef2882301deb4a963c2062cde1fd7",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 17,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:43:43.952455Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "relay-evidence",
      "client_self_reported": true,
      "client_version": "1",
      "duration_ms": 5,
      "entry_hash": "30d1927993f63aca24997bbc44bb6e627e5ef2882301deb4a963c2062cde1fd7",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "4647d36502f2c271342f9cee0b29ae218752e6fdeb33e5248d57a5e94de67b6e",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 16,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:43:43.883679Z"
    },
    {
      "args_sha256": "b24e18a70ec2b1bcb85613441ab9f698e1176eacb19913782cac818488283b25",
      "client_name": "rmcp",
      "client_self_reported": true,
      "client_version": "3.1.0",
      "duration_ms": 7,
      "entry_hash": "4647d36502f2c271342f9cee0b29ae218752e6fdeb33e5248d57a5e94de67b6e",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "0a17e2afe18403d07b3e9f2e514448524e93bc39f66b432b53048c1f23ce875e",
      "result_digest": null,
      "route": "/api/jarvis/tools/fetch",
      "seq": 15,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "fetch",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:42:12.685688Z"
    },
    {
      "args_sha256": "a03c8657a575356de5937110fc9038dafa42d659089628586aa8f8d73924cd16",
      "client_name": "rmcp",
      "client_self_reported": true,
      "client_version": "3.1.0",
      "duration_ms": 10,
      "entry_hash": "0a17e2afe18403d07b3e9f2e514448524e93bc39f66b432b53048c1f23ce875e",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "fbef467f1d4afb8488ef5213acbed7da859e667dd59f0504a3f3c5173a3aff9e",
      "result_digest": "018d59bdeaaeb4976e97c73051fc5c1dffefa9c284df380f0b2ae4e59e0ee93a",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 14,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:42:10.183500Z"
    },
    {
      "args_sha256": "a03c8657a575356de5937110fc9038dafa42d659089628586aa8f8d73924cd16",
      "client_name": "cursor-vscode",
      "client_self_reported": true,
      "client_version": "1.0.0",
      "duration_ms": 8,
      "entry_hash": "fbef467f1d4afb8488ef5213acbed7da859e667dd59f0504a3f3c5173a3aff9e",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "8217a58865b2ebf274ccf7a7371b6b4feab3fce07e54838f011a504679702864",
      "result_digest": "018d59bdeaaeb4976e97c73051fc5c1dffefa9c284df380f0b2ae4e59e0ee93a",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 13,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:41:43.513008Z"
    },
    {
      "args_sha256": "1a91178b5dd399cdcdab7305e5277e2a7a10deb066580948657fca3efbbfc28a",
      "client_name": "codex-mcp-client",
      "client_self_reported": true,
      "client_version": "0.162.0-alpha.17.2",
      "duration_ms": 47,
      "entry_hash": "8217a58865b2ebf274ccf7a7371b6b4feab3fce07e54838f011a504679702864",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "afc81936f39ec80a50a864890f1759699defe69dea37800eac7a8cc587b0d2ab",
      "result_digest": null,
      "route": "/api/jarvis/tools/emr_recall",
      "seq": 12,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_recall",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:40:19.532772Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "codex-mcp-client",
      "client_self_reported": true,
      "client_version": "0.162.0-alpha.17.2",
      "duration_ms": 21,
      "entry_hash": "afc81936f39ec80a50a864890f1759699defe69dea37800eac7a8cc587b0d2ab",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "6a6f126f382bf59cea72a4f65ded5a94de2ec0caa936f272ecbc6c2581901180",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 11,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:40:19.365844Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "codex-mcp-client",
      "client_self_reported": true,
      "client_version": "0.162.0-alpha.17.2",
      "duration_ms": 6,
      "entry_hash": "6a6f126f382bf59cea72a4f65ded5a94de2ec0caa936f272ecbc6c2581901180",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "fbcc90e6eed4f321725aae6c317feb77d068c431da4473265865e6b8b8f26562",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 10,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:39:49.210761Z"
    },
    {
      "args_sha256": "d85e151a557634d3cab592e821d3190203a048098de9bb39453a2bc2f6e3d2dd",
      "client_name": "codex-mcp-client",
      "client_self_reported": true,
      "client_version": "0.162.0-alpha.17.2",
      "duration_ms": 34,
      "entry_hash": "fbcc90e6eed4f321725aae6c317feb77d068c431da4473265865e6b8b8f26562",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "f1e082951bfa483f1616e3ba226788a969eedfd912c76e46bf9b1dc1ff5f1f64",
      "result_digest": null,
      "route": "/api/jarvis/tools/emr_recall",
      "seq": 9,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_recall",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:39:48.172351Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "opencode",
      "client_self_reported": true,
      "client_version": "1.18.35",
      "duration_ms": 21,
      "entry_hash": "f1e082951bfa483f1616e3ba226788a969eedfd912c76e46bf9b1dc1ff5f1f64",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "492a0c2001837a1f6a83071c352c9b3cd389bcfae26ae4dde5fab2fde6b0a383",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 8,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "mcp-stdio",
      "ts": "2026-10-11T00:39:18.759626Z"
    },
    {
      "args_sha256": "b24e18a70ec2b1bcb85613441ab9f698e1176eacb19913782cac818488283b25",
      "client_name": "Python-urllib",
      "client_self_reported": true,
      "client_version": "3.12",
      "duration_ms": 7,
      "entry_hash": "492a0c2001837a1f6a83071c352c9b3cd389bcfae26ae4dde5fab2fde6b0a383",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "83fb8dc402b8bee7c3a162d9b22265b59e3223f8357a8f0add43974da3178a39",
      "result_digest": null,
      "route": "/api/jarvis/tools/emr_fetch",
      "seq": 7,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_fetch",
      "transport": "http-tool",
      "ts": "2026-10-11T00:29:58.579976Z"
    },
    {
      "args_sha256": "ca502dec04523cdc33afece69a9b600d5b9bd022d453791cc693b6b372f808ad",
      "client_name": "Python-urllib",
      "client_self_reported": true,
      "client_version": "3.12",
      "duration_ms": 11,
      "entry_hash": "83fb8dc402b8bee7c3a162d9b22265b59e3223f8357a8f0add43974da3178a39",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "7ba521e3635a60e5bc9f7843c9f0c65534fae6ab34e6c6576078fdd4d06c7e47",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 6,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:29:14.026327Z"
    },
    {
      "args_sha256": "b24e18a70ec2b1bcb85613441ab9f698e1176eacb19913782cac818488283b25",
      "client_name": "Python-urllib",
      "client_self_reported": true,
      "client_version": "3.12",
      "duration_ms": 8,
      "entry_hash": "7ba521e3635a60e5bc9f7843c9f0c65534fae6ab34e6c6576078fdd4d06c7e47",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "878da51e7b1700dbf3f9c5a4660e0cc729d5c9671afd019ab6ee0e97348600ae",
      "result_digest": null,
      "route": "/api/jarvis/tools/emr_fetch",
      "seq": 5,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_fetch",
      "transport": "http-tool",
      "ts": "2026-10-11T00:28:40.152978Z"
    },
    {
      "args_sha256": "a1a1069b2341607e2915fbf9680325ed94640395be9f15bcaf0fff807ef7568b",
      "client_name": "Python-urllib",
      "client_self_reported": true,
      "client_version": "3.12",
      "duration_ms": 21,
      "entry_hash": "878da51e7b1700dbf3f9c5a4660e0cc729d5c9671afd019ab6ee0e97348600ae",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "65c709024f2130a6ae4f00602de1f65eb25ffff57d54b7c5c951816ddc9f83b4",
      "result_digest": "f6d994c9c5e04ab1a7cfbf60b6c9fa5fd60d1ade270abf4141552d642639038f",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 4,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:26:21.489415Z"
    },
    {
      "args_sha256": "dd752d3829d7f1a088ae165ef64035f7e61adb0b39a6ba7d3d782eae0dc960fb",
      "client_name": "Python-urllib",
      "client_self_reported": true,
      "client_version": "3.12",
      "duration_ms": 22,
      "entry_hash": "65c709024f2130a6ae4f00602de1f65eb25ffff57d54b7c5c951816ddc9f83b4",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "91ae8beb8d098d05c6823ee605af0a37827db6a7f6c0a0ea2f69e38b40668414",
      "result_digest": "1d178845bfd853029e098353ba23d3a90d8d6f8f0e61e647b5191dd79d1b2ab3",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 3,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:26:21.422657Z"
    },
    {
      "args_sha256": "44136fa355b3678a1146ad16f7e8649e94fb4fc21fe77e8310c060f61caaff8a",
      "client_name": "Python-urllib",
      "client_self_reported": true,
      "client_version": "3.12",
      "duration_ms": 6,
      "entry_hash": "91ae8beb8d098d05c6823ee605af0a37827db6a7f6c0a0ea2f69e38b40668414",
      "error_code": null,
      "method": "POST",
      "outcome": "ok",
      "prev_hash": "770a1d60362580269bee6de36774f60c10429270ed9f3d04d5beb69e1b56a58a",
      "result_digest": "3bed25d21d893e8ae248b0be92bf6e541c89a3dca84671861cb725392c39f6ae",
      "route": "/api/jarvis/tools/emr_latest",
      "seq": 2,
      "status_code": 200,
      "target": null,
      "tenant": "operator",
      "tool": "emr_latest",
      "transport": "http-tool",
      "ts": "2026-10-11T00:26:21.343969Z"
    }
  ],
  "pages": 1,
  "truncated": false,
  "entries_omitted_from_this_listing": 1
}
```

### `calls_verify` (HTTP 200)

```json
{
  "ok": true,
  "entries": 21,
  "files": [
    "calls-20261011.jsonl"
  ],
  "problems": [],
  "head": {
    "seq": 21,
    "entry_hash": "a5ed81f7fef025e8827912ff6548f926cc3d876ab990e6a3abd7cd825c2e2475"
  },
  "anchor": null,
  "degraded": null,
  "gaps_recorded": 0
}
```
