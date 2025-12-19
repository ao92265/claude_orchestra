# Claude Orchestra - TODO

## High Priority 🔴

### Error Handling & Robustness
- [x] Add comprehensive process cleanup mechanism to prevent orphaned Claude processes
- [ ] Implement state file validation and corruption recovery
- [ ] Add circuit breaker pattern for repeated API/CLI failures
- [ ] Implement graceful shutdown handler for daemon mode
- [ ] Add disk space monitoring before starting cycles

### Testing Infrastructure
- [ ] Create test suite with pytest framework
- [ ] Add unit tests for agent result parsing (brittle regex logic)
- [ ] Add integration tests for full cycle execution
- [ ] Add tests for state persistence and recovery
- [ ] Add tests for concurrent project handling

### Security & Safety
- [ ] Implement audit logging of all file modifications
- [ ] Add pre-commit diff review before auto-commit
- [ ] Create safelist mechanism for allowed files/directories
- [ ] Add `.gitignore` integration to prevent committing secrets

## Medium Priority 🟡

### Dashboard Improvements
- [ ] Add process timeout visual indicators
- [ ] Implement export functionality (logs, stats to CSV/JSON)
- [ ] Add real-time browser notifications for cycle events
- [ ] Create performance metrics visualization (charts)
- [ ] Add advanced log filtering by agent type and keywords
- [ ] Implement historical data view and trend analysis
- [ ] Fix directory browser path traversal vulnerability

### Orchestrator Enhancements
- [ ] Implement agent retry logic with exponential backoff
- [ ] Add partial cycle recovery (resume from failed stage)
- [ ] Implement agent performance profiling and metrics
- [ ] Create smart task selection based on success history
- [ ] Add cycle rollback mechanism for buggy changes
- [ ] Support concurrent PR review in parallel

### Daemon Features
- [ ] Add auto-restart on crash functionality
- [ ] Implement resource limits (CPU/memory constraints)
- [ ] Add webhook notification support (Slack, Discord, etc.)
- [ ] Implement cycle metrics persistence to disk
- [ ] Add health check for project path accessibility

## Low Priority 🟢

### Code Quality
- [ ] Replace all bare exception handlers with specific types
- [ ] Add comprehensive docstrings to all classes and methods
- [ ] Refactor hardcoded strings to configuration files
- [ ] Create input validation helper functions
- [ ] Improve parsing logic robustness with better error messages

### Documentation
- [ ] Create architecture diagram showing component interactions
- [ ] Write error troubleshooting guide for common failures
- [ ] Document all sub-agent types and their purposes
- [ ] Create performance tuning guide
- [ ] Document state file format schema
- [ ] Document dashboard Socket.io API events
- [ ] Write contributing guide with coding standards

### Features
- [ ] Add support for custom agent prompts from config files
- [ ] Create agent prompt versioning system
- [ ] Implement task queue prioritization
- [ ] Add support for project templates
- [ ] Create command-line status viewer (alternative to dashboard)

## Completed ✅
- [x] Add multi-project support to dashboard
- [x] Implement sub-agent tracking and activity logging
- [x] Add model selector (Haiku/Sonnet/Opus)
- [x] Add task mode selector (small/normal/large)
- [x] Fix timeout loop issues with smarter task selection
- [x] Add directory browser to dashboard
- [x] Support multiple pending project tabs
- [x] Add comprehensive process cleanup mechanism to prevent orphaned Claude processes

---

## Task Descriptions

### Process Cleanup Mechanism
Currently, if the daemon crashes or is killed ungracefully, spawned Claude processes continue running. Need to:
- Track all spawned subprocess PIDs
- Register signal handlers (SIGTERM, SIGINT)
- Clean up all child processes on exit
- Add periodic orphan process detection

### State Validation and Recovery
State files can become corrupted, causing crashes. Need to:
- Define JSON schema for state files
- Validate state on load with fallback to default
- Implement auto-backup before state writes
- Add state migration for schema changes

### Test Suite Creation
No tests currently exist. Need to:
- Set up pytest with proper fixtures
- Mock subprocess calls for Claude CLI
- Test parsing logic with edge cases
- Test concurrent project state isolation
- Add CI/CD integration (GitHub Actions)

### Audit Logging
For security and debugging. Need to:
- Log all file read/write operations with timestamps
- Log all git commands executed
- Store logs in project-specific audit.log files
- Add audit log viewer to dashboard

### Dashboard Metrics Visualization
Current dashboard only shows current state. Need to:
- Store cycle duration history in database
- Add Chart.js library for visualization
- Show success rate trends over time
- Display agent performance comparisons
- Export charts as PNG images

---

## Notes

- High priority items focus on stability and safety
- Medium priority items improve UX and productivity
- Low priority items are "nice to have" enhancements
- Tasks marked with testing icon 🧪 require test coverage
- Tasks marked with security icon 🔒 have security implications
