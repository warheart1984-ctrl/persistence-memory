# trust/

`roots.pub` pins the public root keys that every signature in the ledger ultimately depends on. It is a public file kept in the
repository on purpose: the repository, the PC and the Mint box each hold a copy, and a verifier on the PC trusts the PC's copy, never
the Mint box's. A root key can only be added here by whoever can commit to the repository (and, in the ledger, by a statement an
existing root signed), not by whoever can write the ledger's database.

It is empty until the key ceremony (`docs/SIGNATURES.md`). The PRIVATE root keys never go here and never go on the Mint box.
