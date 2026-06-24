# claude-memory v6: Multi-Agent Attribution & Codex Onboarding

**Date:** 2026-05-13
**Version:** v6
**Status:** Design approved; pending implementation plan
**Preceded by:** [v5.2 Backlog Apply](./2026-04-22-v5-2-backlog-apply-design.md) *(corpus state reference)*

## Overview

v6 introduces **agent attribution** to the claude-memory MCP server so multiple agent families (Claude, Codex, and any future additions) can share the same corpus without silently overwriting each other or polluting consolidation. Every write to the corpus is stamped with a `source_agent` (family-level) and `source_client_id` (raw OAuth/API-key identifier). Write tools enforce a **rule-b ownership model** for owned content (only the original author can modify) while leaving shared project metadata as last-writer-wins. The v5 log-time consolidation pipeline gains a **cross-agent skip** so auto-merges and supersedes never cross family boundaries.

Codex is onboarded via a new `api_keys` table that stores hashed bearer tokens with per-client revocation, family classification, and labels. The existing legacy `API_KEY` env-var path is replaced by per-machine `api_keys` rows for Brian's Claude fleet (Workstation Claude Desktop, Claude Code, ai-server, laptop, etc.), giving per-machine revocation and audit. OAuth DCR continues to function for any client that prefers it.

The v6 scope is intentionally narrow: attribution, write enforcement, consolidation skip, and the auth/onboarding plumbing required to make Codex a first-class client. Cross-agent merge investigation, per-agent UI surfaces, and per-machine reporting dashboards are out of scope.

## Motivation

The corpus is ~748 live lessons as of 2026-05-13, all written by Claude. Bringing Codex into the same MCP server raises three risks without attribution:

1. **Silent corpus drift.** With no `source_agent` column, Codex lessons would intermix with Claude's and be indistinguishable in search, retrieval, and consolidation outputs.
2. **Cross-agent auto-merge.** v5 log-time consolidation auto-merges at ≥0.90 cosine. Codex writing in its own voice and style on overlapping topics could trigger auto-merges of legitimately-different lessons — and at minimum, we have no evidence yet that cross-agent supersede is a sound default.
3. **Write conflicts on owned content.** Without an ownership rule, Codex could retire or update Claude's lessons (or vice versa), and there's no record of who did what.

A second-order goal is **operational hygiene**: the current auth model uses a single shared bearer token inline in every Claude client's MCP config. That makes rotation expensive and per-machine revocation impossible. v6 fixes this for all clients at the same time as adding Codex.

## Design Decisions

| # | Decision | Chosen |
|---|---|---|
| 1 | Identity granularity | **Hybrid**: family-level `source_agent` for everything user-facing and for consolidation logic, plus a `source_client_id` audit field stamped on every write. |
| 2 | Cross-agent write permissions | **Rule b (mixed)**: agents may only modify rows where `source_agent` matches their own family, with explicit exceptions for `annotate` and `rate_lesson` (anyone may annotate/rate any row). |
| 3 | Owned vs. shared categorization | **Two buckets** — *owned content* (lessons, patterns, journal, agent_specs, specifications, mcp_servers, mcp_server_tools, annotations, ratings) enforces rule b; *shared metadata* (projects, project_state, approaches, key_files, guardrails, permissions, project_aliases, machines, databases, containers, conflicts, mcp_server_projects) is last-writer-wins with attribution. |
| 4 | Consolidation skip scope | **Skip at production mutation points** (log-time consolidation, v5.2 backlog apply); **do not skip in v5.1 analyzer** (tag pairs with a derived `cross_agent` boolean for later investigation). |
| 5 | Codex onboarding | **Static bearer token via new `api_keys` table** (preferred per Codex's own recommendation). OAuth DCR remains available as a future convenience path. |
| 6 | Admin tools (`merge_projects`, `resolve_conflict`) | **Require `'admin'` scope.** No agent has it by default; granted manually per-token when needed. |
| 7 | Unknown OAuth DCR clients | **Lenient**: classified as `family='unknown'`, logged at WARNING with full DCR metadata, can only modify their own rows. |
| 8 | Claude migration strategy | **Per-machine `api_keys` rows** with distinct bearers and descriptive labels, replacing inline shared bearer in MCP configs. |

## Architecture

v6 adds three components and modifies one:

1. **Identity resolver** (`src/identity.py`, new) — single source of truth for `(family, client_id, scopes)` per request. Called from the OAuth provider's `load_access_token` and from the API-key path. Result is attached to per-request context.
2. **Write-stamp + enforcement helpers** (`src/identity.py`) — `stamp(ctx)` returns `(source_agent, source_client_id)`. `assert_can_write(ctx, table, row_id)` raises on rule-b violations.
3. **Admin scripts** (`scripts/issue_api_key.py`, `scripts/revoke_api_key.py`, `scripts/list_api_keys.py`, new) — out-of-band token management against the prod DB. Not exposed as MCP tools.
4. **Consolidation candidate query** (`src/consolidation/candidates.py`, modified + `src/tools/backlog_apply.py`'s `fetch_candidate_rows`) — gains a `WHERE candidate.source_agent = new_lesson.source_agent` clause.

**Integration points:**

- Every write tool gains two lines: read `(family, client_id) = stamp(ctx)`, pass through to INSERT/UPDATE.
- Every owned-content update/retire tool gains one line: `assert_can_write(ctx, 'lessons', lesson_id)` before the mutation.
- Existing OAuth DCR flow is unchanged on the wire; on first sighting of a new OAuth `client_id`, the resolver inserts a row into `oauth_client_family` based on `client_name` prefix.
- Legacy `API_KEY` env-var path is retained for the transition window with a deprecation log line on every hit. Dropped after deprecation warnings cease for 7 consecutive days in prod logs.

## Data Model

### New tables

#### `api_keys`

Static bearer tokens with per-row revocation, family classification, and scopes.

```sql
CREATE TABLE api_keys (
    id              SERIAL PRIMARY KEY,
    api_key_hash    TEXT NOT NULL UNIQUE,
    family          TEXT NOT NULL,
    client_name     TEXT,
    label           TEXT,
    scopes          TEXT[] NOT NULL DEFAULT ARRAY['read','write'],
    created_at      TIMESTAMP NOT NULL DEFAULT NOW(),
    last_seen_at    TIMESTAMP,
    revoked_at      TIMESTAMP
);

CREATE INDEX idx_api_keys_active ON api_keys (revoked_at) WHERE revoked_at IS NULL;
```

- `api_key_hash`: sha256 hex of the raw bearer. Plaintext bearer is shown to the operator once at creation and never stored.
- `family`: enum-like string. Today: `'claude'`, `'codex'`. Future additions append.
- `client_name`: optional, free-form (e.g., `'codex-cli'`, `'claude-desktop-workstation'`). Used for logs.
- `label`: human-readable description for `list_api_keys` output and operator memory (e.g., `'Brian Codex laptop'`, `'Brian Claude ai-server'`).
- `scopes`: simple array. Today: `['read','write']` for regular tokens, `['read','write','admin']` for admin tokens. Future scopes may be added without migration.
- `last_seen_at`: updated on every authenticated request that uses this key. Used to identify dormant tokens for cleanup.
- `revoked_at`: soft-delete. Revoked rows remain for audit but are not accepted for auth.

#### `oauth_client_family`

Family classification for OAuth DCR clients, computed on first sighting.

```sql
CREATE TABLE oauth_client_family (
    client_id       TEXT PRIMARY KEY REFERENCES oauth_clients(client_id) ON DELETE CASCADE,
    family          TEXT NOT NULL,
    client_name     TEXT,
    inferred_from   TEXT NOT NULL,    -- 'client_name_prefix' | 'manual'
    inferred_at     TIMESTAMP NOT NULL DEFAULT NOW()
);
```

- One row per OAuth client_id, inserted on the request immediately following DCR registration.
- `inferred_from = 'client_name_prefix'` for the default rule-based classification; `'manual'` if an operator later updates the row.
- An operator can promote an `unknown` family to `claude`/`codex`/etc. by `UPDATE oauth_client_family SET family = ?, inferred_from = 'manual' WHERE client_id = ?`.

### Modified tables — owned content (rule b enforced)

The following tables add `source_agent TEXT NOT NULL DEFAULT 'claude'` and `source_client_id TEXT` columns:

- `lessons`
- `patterns`
- `journal`
- `agent_specs`
- `specifications`
- `mcp_servers`
- `mcp_server_tools`
- `annotations`

If a `lesson_ratings` (or equivalent) table exists separately from `lessons`, it joins this list. *(Confirmed during implementation planning.)*

### Modified tables — shared metadata (last-writer-wins, attributed)

Same column additions, but rule b is **not** enforced on UPDATE/DELETE — last writer wins, stamp records who wrote last:

- `projects`
- `project_state`
- `approaches`
- `key_files`
- `guardrails`
- `permissions`
- `project_aliases`
- `machines`
- `databases`
- `containers`
- `conflicts`
- `mcp_server_projects`

### Modified tables — audit-only attribution

These tables already encode run/operator context but gain `source_agent` for cross-agent investigation:

- `consolidation_runs`
- `backlog_analysis` (additionally gains a derived `cross_agent BOOLEAN GENERATED ALWAYS AS (left_source_agent <> right_source_agent) STORED` column for future investigation queries)

### Backfill

All existing rows are stamped `source_agent = 'claude'`, `source_client_id = NULL`. The corpus is 100% Claude as of 2026-05-13.

## Identity Resolution

### Per-request flow

The resolver runs once per authenticated MCP request:

1. **Legacy API_KEY path.** Bearer equals `API_KEY` env var → `family='claude'`, `client_id='legacy-api-key'`, `scopes=['read','write']`. Log `DEPRECATION: legacy API_KEY used by <source_ip>` at WARNING level. Path retained until 7 consecutive deprecation-free days in prod logs.
2. **`api_keys` path.** sha256(bearer) matches `api_keys.api_key_hash` where `revoked_at IS NULL` → `family=row.family`, `client_id=f'apikey:{row.id}'`, `scopes=row.scopes`. Update `last_seen_at = NOW()`.
3. **OAuth path.** Bearer is a valid `oauth_access_tokens` row → look up `oauth_clients.client_name` → look up or insert into `oauth_client_family`. If insert: classify by prefix (`claude*` → `claude`, `codex*` → `codex`, else `unknown`), log new classifications at WARNING. `client_id=f'oauth:{oauth_client_id}'`, `scopes=['read','write']` by default.
4. **Otherwise.** Existing 401 path (unchanged).

### Context plumbing

Resolved identity is attached to FastMCP's per-request context. Tools read it via:

```python
def stamp(ctx) -> tuple[str, str]:
    """Return (source_agent, source_client_id) for the current request."""
    return ctx.request_context.lifespan_context.agent_family, ctx.request_context.lifespan_context.client_id
```

Implementation detail: FastMCP's `Context` already exposes `request_context`. We attach `agent_family` and `client_id` to either the lifespan context or a new per-request request-scoped dict, depending on what's cleanest. *(Decided during implementation.)*

### Family prefix rules (initial)

| Prefix match (case-insensitive) | Family |
|---|---|
| `claude*` (`claude-code`, `claude-desktop`, `mcp-remote` *if invoked from a Claude config*) | `claude` |
| `codex*` (`codex-cli`, `codex-app`) | `codex` |
| anything else | `unknown` |

`mcp-remote` is a wrapper invoked by both Claude clients today. In practice the OAuth `client_name` will reflect the wrapping client. If `mcp-remote` registers itself with its own name, we add a rule to classify it as `claude` until/unless other clients also use `mcp-remote`, at which point we'd require finer disambiguation. **For now this is a non-issue** because no Claude client currently uses OAuth DCR (all are on legacy API_KEY path; see Migration Strategy).

## Write Enforcement (Rule b)

### Per-tool patterns

**Pattern 1 — Pure insert (no ownership check, just stamp):**

Tools: `log_lesson`, `write_journal`, `log_pattern`, `create_spec`, `register_agent`, `register_mcp_server`, `register_mcp_tool`, `annotate`, `rate_lesson`, `add_project`, `add_machine`, `add_container`, `start_session`, `end_session`.

Pattern:
```python
family, client_id = stamp(ctx)
await conn.execute(
    "INSERT INTO lessons (..., source_agent, source_client_id) VALUES (..., $N, $N+1)",
    ..., family, client_id,
)
```

**Pattern 2 — Update/retire on owned content (assert + stamp audit fields):**

Tools: `update_lesson`, `retire_lesson`, `update_spec`, `retire_spec`, `update_agent`, `retire_agent`, `update_mcp_server`, `retire_mcp_server`, `clear_annotation`.

Pattern:
```python
await assert_can_write(ctx, 'lessons', lesson_id)
family, client_id = stamp(ctx)
# Original source_agent stays; updated_by/retired_by audit fields get the actor.
await conn.execute(
    "UPDATE lessons SET retired_at = NOW(), retired_by_agent = $1, ... WHERE id = $2",
    family, lesson_id,
)
```

Audit fields like `retired_by_agent`, `updated_by_agent` are added to owned tables as nullable TEXT columns alongside the existing `retired_at` / `updated_at` timestamps. **(Note:** these audit fields are separate from `source_agent`, which never changes after row creation.)

**Pattern 3 — Update on shared metadata (stamp the row's source_agent to the current writer):**

Tools: `update_project_state`, `set_project_claude_md`, `update_project_claude_md`, `add_project` *(if updating existing)*, etc.

Pattern:
```python
family, client_id = stamp(ctx)
await conn.execute(
    "UPDATE project_state SET current_focus = $1, source_agent = $2, source_client_id = $3 WHERE project_id = $4",
    current_focus, family, client_id, project_id,
)
```

### Special cases

- **`approve_consolidation` / `reject_consolidation` / `undo_consolidation`:** with cross-agent skip in place, queue items are always intra-family. Any agent can act on a queue item regardless of which agent originally enqueued it. No rule-b check.
- **`merge_projects` / `resolve_conflict`:** Pattern 3 (shared-metadata write) plus a scope precondition — require `'admin'` in `scopes`. Default tokens (Claude per-machine + Codex) do not get admin. Granted manually via `UPDATE api_keys SET scopes = scopes || 'admin' WHERE id = ?` when needed for a specific operation.
- **Pattern 1 insert into `mcp_server_projects` (junction table):** stamp source_agent on the link row itself. No ownership check on either side — anyone can link any MCP server to any project.
- **`update_project_state` historical writes:** if the existing project_state implementation appends a history row rather than mutating in place, stamp each history row's `source_agent` to the writer. (Confirmed during implementation — v2 schema has a `project_state_history` table.)

### Error surface

`assert_can_write` raises `PermissionError("agent '{family}' cannot modify row owned by '{owner_family}' in {table}")`. FastMCP translates this to a structured tool error response. The error includes the table and the owner family so the caller can decide whether to give up, fork a new lesson, or annotate instead.

## Consolidation Cross-Agent Skip

### Modified queries

`src/consolidation/candidates.py`'s candidate query adds `AND candidates.source_agent = $N` (where `$N` is the new lesson's `source_agent`):

```sql
SELECT id, content, embedding, ...
FROM lessons
WHERE retired_at IS NULL
  AND id <> $1
  AND source_agent = $2          -- NEW
  AND embedding <=> $3 < $4
ORDER BY embedding <=> $3
LIMIT $5;
```

`src/tools/backlog_apply.py`'s `fetch_candidate_rows` applies the same filter at the pair-selection stage.

### v5.1 analyzer — no filter, but tag

`src/consolidation/backlog.py` (the analyzer that produced the original 596-pair report) continues to compare all pairs above the cosine threshold regardless of agent. `backlog_analysis` rows gain the computed `cross_agent` column. Future queries like `SELECT verdict, COUNT(*) FROM backlog_analysis WHERE cross_agent GROUP BY verdict` will produce the raw material for the cross-agent investigation.

### Regression test

A new test asserts: logging a lesson with `source_agent='codex'` does **not** produce a consolidation candidate when the only ≥0.90-cosine neighbor has `source_agent='claude'`. The pre-change query would have returned the neighbor; the post-change query returns empty.

## Read-Side Behavior

All read tools return **unfiltered cross-agent results by default**. The shared corpus is the entire point of multi-agent attribution. Tools affected:

- `search`, `search_lessons`, `search_specs`, `find_context`, `find_mcp_tools`
- `get_project`, `get_connectivity`, `get_agent`, `get_spec`, `get_mcp_server`
- `read_journal`, `list_pending_consolidations`, `list_conflicts`
- `list_projects`, `list_machines`, `list_agents`, `list_specs`, `list_mcp_servers`

Three search-style tools (`search`, `search_lessons`, `read_journal`) gain an optional `source_agent` parameter for inspection (e.g., "show me only Codex's lessons about Flutter"). Default is `None` (return all).

## Onboarding & Migration

### Token issuance (one-time, manual)

`scripts/issue_api_key.py --family <family> --label <label> [--client-name <name>] [--scopes read write]`:

1. Generate raw bearer: `secrets.token_hex(32)`.
2. Compute sha256.
3. INSERT into `api_keys` with hash + metadata.
4. Print raw bearer to stdout **once**.
5. Exit. Operator copies bearer to env var on the target machine.

Output is human-readable:
```
Issued API key for family='codex' label='Brian Codex laptop'
   id: 7
   client_name: codex-cli
   scopes: read, write

Bearer token (store NOW — will not be shown again):
   c3f7a8d9...

Suggested env var name: CODEX_MEMORY_TOKEN
```

### Codex setup

1. Operator runs `scripts/issue_api_key.py --family codex --label "Brian Codex laptop" --client-name codex-cli`.
2. Operator sets `CODEX_MEMORY_TOKEN=<bearer>` in shell environment (or secrets manager).
3. Operator adds to Codex config:
   ```toml
   [mcp_servers.claude-memory]
   url = "https://memory.example.com/mcp"
   bearer_token_env_var = "CODEX_MEMORY_TOKEN"
   ```
4. Codex starts working. Identity resolver classifies it as `family='codex'`. First write triggers no consolidation candidates (Codex has zero prior lessons). Search returns all of Claude's existing context.

### Claude migration (per-machine api_keys rows)

Each machine gets **one shared Claude token** used by all Claude clients on that machine (Claude Desktop + Claude Code + any future Claude client share the same bearer). One row per machine in `api_keys`, not one per client. Per-machine revocation is the goal; per-client distinction is not.

For each machine across the fleet:

1. Run `scripts/issue_api_key.py --family claude --label "Brian Claude <machine>" --client-name "claude-<machine>"`.
2. Set the per-machine env var on that machine (e.g., `CLAUDE_MEMORY_TOKEN` set locally on each machine — same env-var name everywhere, different bearer value per machine).
3. Replace inline `Authorization: Bearer <old-shared>` in `claude_desktop_config.json` and `~/.claude/` configs with a setup that consumes the env var. For `mcp-remote` invocations, the exact mechanism depends on how `mcp-remote` accepts headers — likely a small shell wrapper that reads the env var and constructs the `--header` argument.
4. Verify connectivity (a search call against the MCP server) before deleting the old config.

Machines in scope (confirmed by operator):
- Workstation (shared token for Claude Desktop + Claude Code)
- ai-server (Claude Code)
- Work laptop (Claude Code)
- Any other machine the operator identifies during rollout

### Legacy API_KEY retirement

Once all machines are migrated:

1. Wait for 7 consecutive days with zero `DEPRECATION: legacy API_KEY used` log lines in prod.
2. Remove the legacy path from `src/identity.py`.
3. Unset the `API_KEY` env var in prod and rotate any cached copies.

The shared bearer that was visible in this conversation's transcript should be treated as compromised; rotation is the operative remediation.

### Operational visibility

New admin MCP tool `list_clients` (read-only, requires `'admin'` scope):

```
list_clients() -> list of:
  - source: 'api_key' | 'oauth'
  - id (api_keys.id or oauth client_id)
  - family
  - client_name
  - label (api_keys only)
  - scopes
  - last_seen_at
  - revoked_at (api_keys only)
  - created_at
```

Useful for periodic auditing: "who's connected, who hasn't checked in for a month, who should I revoke."

## Rollout Order

1. **Schema migration** (`migrations/004_v6_attribution.sql`) — create `api_keys`, `oauth_client_family`; add `source_agent` + `source_client_id` columns to all affected tables; backfill to `'claude'`; add audit columns where needed.
2. **Identity resolver** (`src/identity.py`) — implement and unit-test all four resolution branches (legacy, api_keys, OAuth, unauthenticated).
3. **Write stamping** — update every Pattern 1 / 2 / 3 tool to call `stamp(ctx)`. Add unit tests for each tool that the stamped value appears in the row.
4. **Rule-b enforcement** — implement `assert_can_write`; wire it into every owned-content update/retire tool. Tests that cross-agent attempts raise `PermissionError`.
5. **Consolidation skip** — modify candidate queries; add the `cross_agent` derived column to `backlog_analysis`; regression test for the no-cross-agent-match case.
6. **Admin scripts** — `issue_api_key.py`, `revoke_api_key.py`, `list_api_keys.py`. Run against prod DB via existing `DATABASE_URL`.
7. **`list_clients` MCP tool** — admin-scoped read tool.
8. **Issue Codex token** — operator runs the script; Brian configures Codex.
9. **Claude per-machine migration** — issue a token per machine, update each MCP config, verify, switch over.
10. **Monitor** — watch for deprecation log lines, unexpected `client_name` registrations, and rule-b denials in prod.
11. **Retire legacy path** — after 7 deprecation-free days, remove the API_KEY branch from the resolver and rotate the old key.

Steps 1–7 can ship as a single PR; the actual cutover (8–9) is operational; steps 10–11 are post-rollout maintenance.

## Out of Scope

- **Cross-agent merge investigation.** The `backlog_analysis.cross_agent` column will accumulate signal over time. Investigating it is a future v6.x or v7 task. Lesson #837's "after some weeks of accumulated signal" guidance still applies, now extended to "after some weeks of cross-agent signal."
- **Per-agent UI surfaces.** Search results don't badge results by agent; the read-side filter parameter is the only inspection affordance.
- **Cross-machine reporting.** `source_client_id` enables it (per-machine api_keys rows are uniquely identified), but no dashboards or aggregation tools are built in v6.
- **Granular per-tool scopes.** v6 has `read`, `write`, `admin` only. Finer-grained capabilities (e.g., "consolidation reviewer", "spec creator") are deferred.
- **Cross-family annotations as collaboration signal.** Annotations work cross-family today (Pattern 1 insert), but no UI or aggregation surfaces them. The path for "Codex flagged this Claude lesson as outdated" exists; the workflow that uses it does not.
- **Schema for per-agent rate-limiting or quotas.** No anti-abuse mechanism in v6 beyond per-token revocation.

## Open Questions for Implementation

These are decided during the implementation plan, not the design:

1. **Does `lesson_ratings` exist as a separate table or are ratings counters on `lessons`?** Determines whether ratings need their own `source_agent` column.
2. **Does `project_state` have a history table (`project_state_history` from v2)?** Determines whether Pattern 3 writes a new row or updates in place.
3. **Exact mechanism for stamping per-request identity in FastMCP's `Context`.** Either a lifespan-scoped per-request dict or attaching to `request_context` directly. Verified against FastMCP source during Task 2.
4. **`mcp-remote` env-var substitution.** Whether `mcp-remote` accepts `${VAR}` syntax in `--header` arguments, or if a shell wrapper is needed for the Claude per-machine migration. Resolved during Step 9 by testing both paths on Workstation first.

## Success Criteria

- Codex can call any read tool against the MCP server and see the shared corpus.
- Codex's writes are stamped `source_agent='codex'`. Search results include them.
- Codex attempts to `retire_lesson` on a Claude-authored lesson are rejected with a clear error.
- A new Codex lesson at ≥0.90 cosine to an existing Claude lesson does not auto-merge and does not enter the pending queue.
- The v5.1 analyzer (if re-run) tags cross-agent pairs in `backlog_analysis.cross_agent`.
- Every Claude machine connects via its own `api_keys` bearer with no inline shared secret.
- Deprecation log line stops firing within 1 week of cutover.
- `list_clients` returns rows for all expected machines and Codex.
