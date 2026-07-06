# Migrations

Two directories exist for historical reasons; apply in this exact order on an
**existing** database. A **fresh** database needs none of these —
`db/schema.sql` is the complete bootstrap (regenerated from this chain; see
its header for the regeneration procedure).

| # | File | Era |
|---|------|-----|
| 1 | `../../migrations/001_initial_schema.sql` | v1 base (includes journal) |
| 2 | `../../migrations/002_v2_features.sql` | v2: CLAUDE.md, lifecycle, aliases |
| 3 | `../../migrations/003_v3_codified_context.sql` | v3: agents, specs, MCP registry |
| 4 | `v4_feedback_loop.sql` | v4: ratings, annotations, tsvector |
| 5 | `v5_consolidation.sql` | v5: merges, conflicts, queue |
| 6 | `v5_1_backlog_analysis.sql` | v5.1: backlog analysis |
| 7 | `v5_oauth_persistence.sql` | v5: OAuth state persistence |
| 8 | `v6_attribution.sql` | v6: source_agent/client, api_keys |
| 9 | `v7_complement_verdict.sql` | 2026-07-06: judge complement bucket |
| 10 | `v8_client_last_seen.sql` | 2026-07-06: oauth last-seen |
| 11 | `v9_recency_ranking.sql` | 2026-07-06: memory_rank + semantic_search v2 |

`001_add_journal.sql` is a redundant IF-NOT-EXISTS predecessor of the journal
table already created in `001_initial_schema.sql`; skip it.

There is no migration runner or ledger table yet — apply manually with
`psql -v ON_ERROR_STOP=1 -f <file>` and update `db/schema.sql` afterwards
(CI bootstraps from it, so drift fails the build).
