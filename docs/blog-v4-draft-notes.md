# Blog Post Draft: Claude Memory V4 — Closing the Feedback Loop

**Working title options:**
- "Teaching AI Agents to Rate Their Own Knowledge"
- "When Memories Go Wrong: Adding Feedback Loops to AI Agent Memory"
- "Claude Memory V4: What We Learned from Context Hub"

---

## The Story Arc

V4 was inspired by studying someone else's approach to a related problem. Andrew Ng's team published [Context Hub](https://github.com/andrewyng/context-hub) — an open-source CLI tool and registry that gives coding agents curated, versioned API documentation with a built-in feedback loop. We studied it, identified three patterns that addressed known gaps in our system, and adapted them to our architecture.

This post is about what we learned, what we borrowed, and what we built differently.

---

## The Problem We Hadn't Solved

After v3 (the "Codified Context Infrastructure" update, inspired by [Vasilopoulos's paper](https://arxiv.org/abs/2502.00359) on managing knowledge for AI agents), claude-memory had 44 tools, a three-tier architecture (hot/warm/cold), and semantic search across everything. But there was a gap we'd identified in the v2 retrospective and still hadn't addressed:

**What happens when a lesson is wrong?**

A lesson logged six months ago might be outdated. A workaround for a bug that's since been fixed. An approach that turned out to be fragile. The lesson sits in the database with the same embedding, the same similarity score, and the same ranking as every correct lesson around it. There was no mechanism for an agent to say "I followed this lesson and it didn't work" — short of explicitly calling `retire_lesson`, which requires knowing the lesson is wrong before you follow it.

This is the "self-improving loop" problem. Without feedback, memory systems accumulate noise over time.

---

## What Context Hub Does Differently

Context Hub (created by [Andrew Ng](https://github.com/andrewyng) and team) takes a fundamentally different approach from claude-memory. It's a **documentation registry** — a curated, versioned collection of API docs and skills that agents search and fetch. Where our system stores experiential knowledge (lessons learned from doing work), theirs stores reference knowledge (how APIs work, best practices).

But three of their mechanisms caught our attention:

### 1. Annotations — Local Learning

When a Context Hub agent discovers something about a document — "webhook verification needs the raw request body" — it can attach a persistent annotation. The next time any agent fetches that document, the annotation appears automatically. Knowledge compounds across sessions without modifying the original content.

### 2. Feedback — Quality Signals

Agents can upvote or downvote documentation with structured labels (`outdated`, `inaccurate`, `helpful`). This feedback flows to doc maintainers, creating a loop where content improves based on real usage.

### 3. BM25 Search — Keyword Precision

Their search uses BM25 (term frequency scoring) with field weighting — names weighted 3x, tags 2x, descriptions 1x. This is a well-understood information retrieval algorithm that excels at exact keyword matching, complementing the fuzzier semantic search approaches.

---

## How We Adapted These Patterns

We couldn't just copy Context Hub's approach. Our architectures are different — they're a CLI-first documentation registry; we're an MCP-first experiential memory system. But the underlying patterns translated:

### Lesson Ratings (from Feedback)

Context Hub's structured labels (`outdated`, `inaccurate`, etc.) made sense for their public registry where feedback goes to doc authors. For our single-user system, we simplified to **up/down voting** with an optional comment.

The key design decision: **what happens to downvoted lessons?** We considered three options:

1. **Advisory only** — show warning badges, manual retirement
2. **Semi-automatic** — auto-retire after N downvotes
3. **Scored ranking** — factor votes into search ordering, never delete

We chose option 3. A lesson's **confidence score** adjusts its search ranking:

```
confidence = 0.5 + (upvotes / (upvotes + downvotes) * 0.5)
```

No votes = 1.0 (neutral). All downvotes = 0.5 (halves the effective similarity). This aligns with a principle from our earlier [Resonance research](link-to-resonance-if-published): **stable memories outperform churning ones**. Decay should be triggered by real-world contradiction, not arbitrary thresholds.

### Polymorphic Annotations (from Local Learning)

Context Hub stores annotations as JSON files in `~/.chub/annotations/`, keyed by document ID. We adapted this to a **database-backed polymorphic model** — a single `annotations` table with `entity_type` and `entity_id` columns that can attach notes to any entity in the system:

- Lessons ("this workaround only applies to docker-compose v1")
- Specs ("the auth flow changed in the Feb refactor")
- Agent definitions ("needs updating for the new API")
- Projects, MCP servers, MCP tools

Annotations auto-inject into retrieval responses. When you call `get_spec` or `get_agent`, any annotations for that entity appear in the response without a separate lookup. Knowledge surfaces where it's needed.

When a lesson is downvoted with a comment, the comment automatically becomes an annotation — so the *reason* for the downvote is visible the next time anyone reads that lesson.

### Hybrid Search (from BM25)

Our search was 100% semantic — OpenAI ada-002 embeddings with pgvector cosine similarity. This works well for natural language queries ("how do I deploy to production?") but poorly for exact terms ("ContainerConfig KeyError"). A lesson containing that exact string might rank below semantically similar but textually different results.

Context Hub uses standalone BM25. We chose a **hybrid approach** — PostgreSQL's built-in full-text search (`tsvector`/`tsquery`) as a **boost** on top of semantic similarity:

```
effective_score = (semantic_similarity + keyword_boost) * confidence
```

Where `keyword_boost = ts_rank(tsv, query) * 0.3` when keywords match, 0 otherwise.

This required adding `tsvector` columns and auto-populate triggers to all 7 searchable tables, with weighted fields (titles/names get higher weight than body content). The result: searching "docker ContainerConfig" now surfaces the exact lesson about the docker-compose v1 ContainerConfig bug with an effective score of 1.126 — boosted above the 0-1.0 semantic similarity range by the keyword match.

No changes to tool signatures. Callers don't know or care that search got smarter.

---

## The Numbers

| Metric | V3 | V4 |
|--------|----|----|
| Total tools | 44 | 48 |
| Search method | Semantic only | Semantic + keyword boost |
| Lesson quality signals | None (manual retire) | Up/down rating → confidence scoring |
| Entity annotations | Not possible | Any entity, auto-injected |
| Cataloged MCP tools | 79 | 83 |

---

## What We Didn't Take

Not everything from Context Hub made sense for our system:

- **CLI-first model** — We're MCP-first, which is the right call for agent integration
- **CDN distribution / multi-source** — We're a single-user centralized system
- **Language/version variants** — Not applicable to experiential knowledge
- **Structured feedback labels** — Overkill for our use case; up/down is sufficient
- **Public registry model** — Our knowledge is personal and project-specific

The lesson: **study other systems for patterns, not implementations**. The same problem (knowledge quality over time) can have very different solutions depending on architecture and use case.

---

## What's Next

The feedback loop is now closed at the individual lesson level. Three areas remain open:

1. **Automatic memory formation** — Most sessions still don't produce explicit `log_lesson` calls. The biggest knowledge gap is things worth remembering that nobody logged.

2. **Cross-lesson contradiction detection** — Two lessons might give conflicting advice. Today this requires a human to notice. Could the system detect when a new lesson contradicts an existing one?

3. **Memory consolidation** — After hundreds of lessons, related knowledge should merge. Five lessons about Docker deployment gotchas could become one comprehensive spec.

These are the hard problems. V4 gives us the feedback signal to know *which* memories are valuable. The next challenge is using that signal at scale.

---

## Attribution

V4 was directly inspired by studying [Context Hub](https://github.com/andrewyng/context-hub) by [Andrew Ng](https://github.com/andrewyng) and the AI Suite team. The annotation, feedback, and BM25 search patterns from their project shaped our design, adapted to our MCP-based architecture.

V3's three-tier architecture was inspired by Vasilopoulos et al.'s paper ["Managing Knowledge for AI Agents in Large Codebases"](https://arxiv.org/abs/2502.00359).

Claude Memory is open source at [github.com/bjhengen/claude-memory](https://github.com/bjhengen/claude-memory).

---

## Technical Details

For those interested in the implementation:

- **Database:** PostgreSQL 16 with pgvector (semantic) and tsvector (keyword) search
- **Embedding model:** OpenAI text-embedding-ada-002 (1536 dimensions)
- **Full-text search:** PostgreSQL `tsvector` with `setweight` for field priority (A=titles, B=content)
- **Scoring:** `effective_score = (1 - cosine_distance + ts_rank * 0.3) * confidence`
- **Migration:** Single idempotent SQL file — ALTER TABLE for new columns, CREATE TABLE for annotations, CREATE TRIGGER for tsvector auto-population, backfill UPDATE for existing data, CREATE OR REPLACE FUNCTION for updated semantic_search
- **Design docs:** `docs/plans/2026-03-07-v4-feedback-loop-design.md` and `docs/plans/2026-03-07-v4-implementation.md`
