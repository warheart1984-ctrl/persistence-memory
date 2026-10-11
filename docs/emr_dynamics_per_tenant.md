# EMR reinforcement overlay: per tenant

**Found by the isolation matrix** (`tests/test_tenant_isolation.py`): `GET /api/jarvis/memory/emr/status` reported every tenant's reinforcement entries (memory ids, outcome ids) to any tenant, because the overlay (`app/emr.py`, `_REINFORCEMENT`) was one process-global map keyed by memory id only, saved to one shared sidecar file (`data/emr-dynamics.json`).

## Now
* The overlay is keyed the way short-term memory already is: the **operator's** entries by the bare memory id, a **tenant's** by `<tenant>\x1f<memory id>`. `get_reinforcement`, `reinforce_ids`, `correct_memory_ids`, scoring (`activate`), the status view, `amul_gc` and the sidecar all go through the keyed accessors. Nothing reads or reports another tenant's entries; the same outcome id in two tenants is not a replay; a correction in one tenant does not reset another's; a record of the same id in two tenants has two independent states.
* `emr/status` reports this tenant's entries and counts only, and shows the sidecar's **server path to the operator only** (a tenant gets `null`).
* The sidecar is `emr-dynamics-v2`: the operator's entries stay under `"reinforcement"` (flat, exactly where every earlier reader looks, so rolling the code back still reads the file), and every other tenant's are under `"tenants": {<tenant key>: {<memory id>: state}}`.

## Migration of the existing shared file
No migration step to run. A file written before the split (`emr-dynamics-v1`) has only the flat `"reinforcement"` map, and the shared deployment has exactly one tenant, the operator, so those entries **are** the operator's: they load as the operator's and nobody else can see them. The first time such a file is rewritten, a copy of it as it was is kept as `data/emr-dynamics.json.pre-tenant.bak` (made once, never overwritten), which makes the relabelling reversible. If a deployment ever had OAuth tenants writing to the old shared file, their entries cannot be told apart from the operator's (the old file recorded only the memory id): they would load as the operator's and be invisible to the tenants; the overlay is retrievability only (never truth), so the cost is a reset of that tenant's salience, not any loss of data.

Tests: `tests/test_emr_tenant_dynamics.py`, and the isolation matrix, whose two strict expected failures for this became plain tests.
