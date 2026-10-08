"""Digital Twin chat — governed conversational interface to the Continuity Ledger.

Stack position (declared architecture):

    Continuity Ledger -> truth: governed memory, Clause V, conflicts
    EMR               -> governed excitation: abstention + conflict membrane
    twinchat          -> conversation: sessions, prompts, receipts, extraction
    ChatBackend       -> reasoning surface: llm-gateway first, adapters after

Every turn is receipted (append-only SQLite, digest-chained) and every
model sentence must cite a recalled memory or it is dropped. The gate is a
mechanical filter, not semantic entailment verification — see gate.py.
The service writes no twin-attributed records; only eligible user-attributed
decisions persist, as drafts, with evidence pointing at the turn receipt.
"""
