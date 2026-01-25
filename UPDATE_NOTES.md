# Claude Memory Update - January 25, 2026

## Changes in This Update

### 🎯 Primary Fix: Add Missing Journal Tools
- **Issue:** `write_journal` and `read_journal` tools were in the code but not available in the running container
- **Solution:** Rebuild container to pick up the journal tools (added in commit 8133f9b)
- **Database:** Journal table already exists and is ready to use ✓

### 📦 Dependency Updates
- **MCP SDK:** Upgraded from 1.0.0 to 1.28.0
  - Includes fixes for HTTP transport issues
  - Better handling of client disconnects
  - Improved session management

### 🏥 Health Check Improvements
- Added proper `/health` endpoint with database connectivity check
- Added `/ready` endpoint for container readiness probes
- Returns JSON with service status and database connection state

### 📊 Better Error Logging
- Added structured logging with Python's logging module
- Reduced noise from `BrokenResourceError` (client disconnects now logged at DEBUG level)
- Better error context in logs for actual issues

### 🛡️ Error Handling
- Improved middleware error handling
- Graceful degradation when clients disconnect mid-request
- Better separation between expected connection issues and real errors

## Data Safety

✅ **No data loss risk** - All data is stored in Docker volume `claude_memory_data`
✅ **Database unchanged** - Only container code is updated
✅ **Verified:** 29 lessons, 5 sessions, all tables intact

## Tools Now Available

After update, all 21 tools will be available:
- ✅ search, search_lessons
- ✅ get_project, list_projects
- ✅ get_connectivity, list_machines
- ✅ log_lesson, log_pattern
- ✅ start_session, end_session
- ✅ update_project_state
- ✅ check_guardrails
- ✅ add_machine, add_container, add_project
- ✅ get_permissions
- ✅ **write_journal** (NEW - now available!)
- ✅ **read_journal** (NEW - now available!)

## Deployment Steps

1. Stop current containers
2. Rebuild with updated code and dependencies
3. Restart containers
4. Verify all tools are available
5. Test write_journal functionality

Estimated downtime: ~2 minutes
