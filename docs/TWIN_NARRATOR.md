# Twin Narrator

A governed narrator for the AI twin. The model **never decides facts** — it
verbalizes a deterministic `TwinState.v1` and every sentence it writes is
checked against that state before anything is shown.

```
tenant-scoped records → twin engine → TwinState → adapter → gate → sections + receipt
```

Off by default. No writes. No new dependencies (adapters use the existing
`httpx` runtime dep — the same client `amul_llm` uses).

## Flags

| Env var | Effect |
|---|---|
| `JARVIS_TWIN_ENABLED` | Enables `/api/jarvis/twin/state` and `/api/jarvis/twin/providers`. Off → 404 (dark). |
| `JARVIS_TWIN_NARRATOR_ENABLED` | Enables `/api/jarvis/twin/narration` (requires the twin flag too). Off → 404. |

Protected deployments still apply: when `JARVIS_PROTECT_LEDGER_READ` is on and
OAuth is off, the operator key is required for all three routes.

## Endpoints

| Route | Flags | Behavior |
|---|---|---|
| `GET /api/jarvis/twin/state` | `JARVIS_TWIN_ENABLED` | `{state: TwinState.v1}` for the caller's tenant only. |
| `GET /api/jarvis/twin/providers` | `JARVIS_TWIN_ENABLED` | `[{name, adapter, model}]` — **names and models only**, never URLs or key names. |
| `GET /api/jarvis/twin/narration?provider=<name>` | both flags | `{state, narration, receipt}`. Unknown provider → `400 NARRATOR_UNKNOWN`. |
| `GET /ui/twin` (+ `index.html` `app.js` `styles.css`) | `JARVIS_TWIN_ENABLED` | Read-only dashboard. Filename allowlist — anything else → 404. |

## UI (`ui/twin/`)

A zero-dependency static dashboard: left column renders every TwinState
field (index, component meters with `role="meter"` and numeric labels —
color never the only signal — projects, accomplishments, risks, stale
commitments); right column renders the five gated narration sections with
their cite paths and a `<details>` receipt panel listing digests,
`fallback_used`, and the drop table (`{section, reason}` — receipts carry
no dropped text, so the page cannot accidentally render it).

- No write controls, no API-key handling; all DOM writes are `textContent`.
- Loading / error (404 → "endpoints disabled") / empty-ledger states.
- Accessible: real headings, `<main>`/`<section>` landmarks, native
  `<select>`/`<button>`/`<details>`, `aria-live` status region, visible
  `:focus-visible`, AA contrast palette.
- Serve the folder any way you like; the app serves it at `/ui/twin`
  behind `JARVIS_TWIN_ENABLED` (404 when dark).

## Provider configuration (environment only — never request params)

```
JARVIS_TWIN_PROVIDERS='[
  {"name": "local-llama", "adapter": "ollama", "model": "llama3.1"},
  {"name": "gw", "adapter": "llm_gateway", "base_url": "http://127.0.0.1:9000",
   "model": "amul", "api_key_env": "AMUL_LLM_KEY"}
]'
```

Or shorthand for a single provider:

```
JARVIS_TWIN_NARRATOR=ollama JARVIS_TWIN_MODEL=llama3.1
JARVIS_TWIN_BASE_URL=http://localhost:11434 JARVIS_TWIN_API_KEY_ENV=MY_KEY
```

- `adapter` ∈ `openai_compatible` | `anthropic` | `google` | `ollama` | `llm_gateway` | `none`
- `openai_compatible` covers every `/chat/completions` server (OpenAI, xAI/Grok,
  Groq, Together, Mistral, DeepSeek, OpenRouter, LM Studio, vLLM, llama.cpp).
- `api_key_env` names the env var holding the key — the key itself is never in
  config, URLs, receipts, logs, or the ledger. Missing env var → `NARRATOR_NO_KEY`
  → template fallback.
- `base_url` must be localhost or in `JARVIS_TWIN_ALLOWED_URLS` (comma-separated).
  Anything else → `NARRATOR_URL_NOT_ALLOWED`.
- `none` (the template narrator) is always configured and is the default.

## The gate

`app/narrator/gate.py` checks **every sentence, clause by clause**, before it
renders. A sentence is `{text, cites: [paths into TwinState]}`; every cite must
resolve. Drop reasons:

| Reason | Trigger |
|---|---|
| `BAD_JSON` | output isn't the `{sections: {...}}` contract |
| `CITE_MISSING` | cite path doesn't resolve in the state |
| `NUMBER_MISMATCH` | a numeric literal isn't backed by a cited value (exact int, ±5e-3 float, `40%` ≡ `0.40`, or the state's own rounding) |
| `ENTITY_UNSUPPORTED` | a state entity (record id / subject / tag / project) appears outside its cites; a sentence word merely *contains* a cited word (`gate` cited ≠ `gateway` written) |
| `UNSUPPORTED_TEXT` | a domain word is absent from cited values/schema labels, or the sentence is not a cited extract or one of the closed render forms whose fields are checked against the state |
| `HEDGE_CLAUSE` | a clause carries a hedge/speculation marker (`let's assume`, `suppose`, `probably`, `maybe`, `imagine`, `it seems`, …). Markers inside verbatim-cited record text are data and pass |
| `CLAIM_WORD` | `proven / verified / complete / secure / guaranteed / merged / deployed / fixed` unless the word appears verbatim in a cited value |
| `URL_UNSUPPORTED` | a URL that isn't inside a cited value |

Clause rule (from cslm-genesis): a hypothetical marker scopes **rightward** —
it can never rescue asserted text before it. Hedge words inside
verbatim-cited record text are *data*, not model speculation, and pass.

The gate is not a natural-language entailment model. It accepts source-text
extracts and a finite set of narrator templates with values checked against
their cited fields; it rejects open-ended paraphrases. Citations establish
which stored values were used, not whether those values are true.

Dropped sentences are logged `{section, index, reason}` in the receipt and
never reach the response body. Any section the gate empties is filled by the
deterministic template, flagged `template: true` on each item.

## Fallback ladder

`none` → template. Model down / timeout / non-2xx / bad JSON / every sentence
dropped → template for the missing sections. The receipt records
`fallback_used` and per-sentence drops.

## Receipt — `TwinNarrationReceipt.v1`

`{state_digest, twin_input_digest, provider, model, prompt_digest,
raw_output_digest, final_output_digest, dropped, fallback_used, latency_ms}`

Digests are SHA-256 over prompts and output text only. **No API key value can
appear** — digests cover text, and keys never enter text. The receipt is
returned to the caller and not persisted in this phase (persist lands later,
behind a witness).

## What this proves / does not prove

**Proven by tests:** template is gate-clean over 500 random ledgers; every
drop reason fires; hedged tails cannot rescue unsupported heads; `gate` does
not back `gateway`; only the literal tag `risk` feeds `open_risks`; adapters
emit the documented request shapes; timeout/non-2xx/malformed/missing-key all
fall back; unknown providers and unlisted URLs are refused; tenant-scoped
state/narration; narration writes nothing.

**Not proven:** models may still produce *boring* prose — the gate bounds
claims, not quality. `source_agent` remains caller-supplied (see
`docs/AI_TWIN.md`). Remote providers need an explicit `JARVIS_TWIN_ALLOWED_URLS`
entry — that's deliberate.
