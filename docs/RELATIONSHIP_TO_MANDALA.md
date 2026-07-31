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
