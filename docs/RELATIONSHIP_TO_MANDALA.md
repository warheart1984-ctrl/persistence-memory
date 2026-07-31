# Relationship to Mandala `jarvis-memoryboard/`

**Status:** evidence-bound mapping — not equivalence.

| Aspect | This repo (`persistence-memory`) | Mandala `jarvis-memoryboard/` |
|--------|----------------------------------|-------------------------------|
| Role | Standalone Continuity Ledger distribution | Workspace-embedded Continuity Ledger + Cursor hooks |
| Schema | `continuity-ledger-v1` | Same schema lineage |
| API prefix | `/api/jarvis/memory` | Same (client-compatible) |
| CCS / CES / RC | **Not claimed enforced** here | Mostly **declared** in Mandala docs |
| Hooks | Not required to run the service | Mandala Cursor session hooks |
| CI / Docker | Present in this repo | Historically local-only |

**Do not invent:** deploying this repo does **not** install Mandala constitutional engines, CCS root authority, or Drive-G governance runtime. Continuity unifies evidence records; it does not adjudicate domain truth.

## Drift snapshot (crew2, 2026-07-30)

Hash compare of `app/*` (PM = SoT for platform hardening):

| File | Result | Notes |
|------|--------|-------|
| `models.py` | IDENTICAL | Shared ledger schema |
| `continuity.py` | IDENTICAL | Selections / conflict helpers |
| `store.py` | DRIFT | PM uses atomic `os.replace` temp write |
| `main.py` | DRIFT | PM: dotenv, `OptionalApiKeyMiddleware`, distribution metadata, import-at-top for `to_selection` |
| `auth.py` | MISSING in Mandala | Optional API key — PM only |
| `__main__.py` | DRIFT | PM: `JARVIS_ENV=production` disables reload |

**Sync policy:** Prefer fixes in **persistence-memory**. Mandala sync is an optional later operator action (port atomic store, auth, prod reload gate). Do not weaken PM to match Mandala.

## Clause V

See `docs/CLAUSE_V_HYGIENE.md`. Mandala Boundary Clause V is referenced for lineage only; it is **not** runtime-enforced in this API.
