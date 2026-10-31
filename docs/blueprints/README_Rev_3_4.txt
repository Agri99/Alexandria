RAPHAEL + ALEXANDRIA
LEAN PRODUCTION READINESS BLUEPRINT — REVISION 3.4
Date: 25 October 2026

CONTENTS
- Raphael_Production_Blueprint_Runbook_Rev_3_4.pdf / .docx / .md
- Alexandria_Production_Blueprint_Runbook_Rev_3_4.pdf / .docx / .md

PURPOSE
These runbooks provide a phased, testable implementation plan for the Raphael Android companion/restricted laptop gateway and the standalone Alexandria shared-memory library/database.

PRIORITY TIERS
Tier 1 — MUST: correctness, data safety, security boundaries, real-device path, and a verified restore drill.
Tier 2 — SHOULD: measured fast/deep behavior, supported Python matrix, migration/restart testing, repository sync, and compact English/Indonesian behavior checks.
Tier 3 — COULD: larger toolchain upgrades, optional evaluation expansion, and other improvements that do not block the core safety/reliability gate.

IMPORTANT SCOPE DECISIONS
- Treat the SQLite Online Backup API as the canonical full-database backup/restore path. Remove JSON restore/import if there is no concrete supported use case; JSON export can remain as a separate portable export feature.
- Add an active-memory partial unique index only after checking and safely resolving existing active duplicates; test concurrent writes and restore/import behavior.
- Reuse exact SSE delta/done/error fixtures to test both Python gateway event production and the Kotlin stream parser.
- Validate the Android production manifest/configuration with cleartext traffic disabled. Debug-only exceptions must not leak into release builds.
- Keep device-specific hostnames and secrets out of committed defaults. Use local ignored configuration/environment variables and rotate secrets when exposure is plausible.
- Do not hand-write cryptographic wrappers. Defer a secret-store migration until a maintained approach and a tested migration plan are available.
- `fast` and `deep` must represent a measured and documented behavior/cost distinction or be renamed honestly; identical model IDs with different token budgets do not automatically establish a quality distinction.

BASELINE SNAPSHOTS REVIEWED
- Raphael: 43dd995462963fb3191db95334a0e57dbc9bd982
- Alexandria: 0fffa6667aeab05bf0d8a8cd2587afe4ce5abc23

This is an implementation blueprint, not certification that every control is already implemented or passed. Re-check the repositories and current CI status before starting each phase; paths or technical details may change as the code evolves. A green CI run is necessary but is not a substitute for the real-device integration test, backup restore drill, and signing-key backup.
