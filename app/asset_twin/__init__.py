"""Load-bearing digital twin with human veto — simulation-only reference.

Diagram blocks implemented here:

  Physical System (simulated) -> Twin Core -> Decision Support -> Human Veto
    -> Execution (simulator only) / Safe State + Escalation
    + Evidence, Governance, Traceability underneath everything.

Simulation-only: the "physical system" is ``simulator.SimulatedAsset``,
an in-process deterministic model. Execution writes only to that simulator.
There is no driver, no socket, no GPIO, no SCADA path. Do not wire this to
a real asset: it is not certified, not redundant, and not safety-reviewed.
Advisory + training + integration pattern only.
"""
