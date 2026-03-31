# CLAUDE.md Updates for V4

Apply these three changes to CLAUDE.md on slmbeast and work laptop.

---

## Change 1: Key Tools table

Find the `### Key Tools` table. Update the `search` description and add two new rows after `log_lesson`:

```
| `search` | Semantic + keyword search across all memories |
| `get_project` | Full project context (state, files, approaches) |
| `log_lesson` | Save a lesson learned |
| `rate_lesson` | Up/down vote a lesson (affects search ranking) |
| `annotate` | Attach a note to any entity (lesson, spec, agent, etc.) |
| `get_connectivity` | Infrastructure details (SSH, containers, DBs) |
```

---

## Change 2: "During work" section

Find `**During work:**` and add two lines after `search_lessons`:

```
**During work:**
- `search_lessons` when hitting a problem - someone may have solved it before
- `rate_lesson` when a lesson helped (`up`) or was wrong/outdated (`down`) - ratings affect future search ranking
- `annotate` to attach a note to any entity (lesson, spec, agent, project, mcp_server, mcp_tool)
- `get_connectivity` to find SSH commands, container names, database details
- `check_guardrails` before destructive operations
```

---

## Change 3: Version History

Add this entry BEFORE the V3 entry:

```
- **2026-03-07:** V4 Feedback Loop & Search Improvements
  - 48 tools total (4 new: rate_lesson, annotate, get_annotations, clear_annotation)
  - Hybrid semantic+keyword search with confidence-weighted ranking across all search surfaces
  - Lesson ratings (up/down) affect search ordering — low-rated lessons sink, never auto-deleted
  - Polymorphic annotations on any entity, auto-injected into get_spec/get_agent responses
  - MCP catalog: 6 servers, 83 tools discoverable via find_mcp_tools
```
