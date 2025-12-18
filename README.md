# Claude Orchestra 🎭

A multi-agent development pipeline that orchestrates multiple Claude Code CLI instances to autonomously handle different parts of the software development lifecycle.

## Concept

```
┌─────────────────────────────────────────────────────────────────┐
│                    CLAUDE ORCHESTRA                              │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│   ┌──────────────┐     ┌──────────────┐     ┌──────────────┐   │
│   │ IMPLEMENTER  │────▶│    TESTER    │────▶│   REVIEWER   │   │
│   │              │     │              │     │              │   │
│   │ • Read TODO  │     │ • Run tests  │     │ • Review PR  │   │
│   │ • Implement  │     │ • Fix bugs   │     │ • Approve or │   │
│   │ • Commit     │     │ • Create PR  │     │   Request    │   │
│   └──────────────┘     └──────────────┘     └──────┬───────┘   │
│                                                     │           │
│                              ┌──────────────────────┘           │
│                              │ (can run in parallel)            │
│                              ▼                                  │
│                        ┌──────────────┐                         │
│                        │   PLANNER    │                         │
│                        │              │                         │
│                        │ • Analyze    │                         │
│                        │ • Ideate     │                         │
│                        │ • Add TODOs  │                         │
│                        └──────────────┘                         │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

## Features

- **Four Specialized Agents**: Each handles a specific part of the dev cycle
- **Async Parallel Execution**: Reviewer and Planner run simultaneously
- **State Persistence**: Tracks cycles, branches, and PRs across runs
- **Configurable**: Custom timeouts, models, and prompts
- **Safe**: Uses subprocess with argument lists (no shell injection)

## Installation

1. Ensure Claude Code CLI is installed and authenticated:
   ```bash
   # Install Claude Code
   npm install -g @anthropic-ai/claude-code

   # Authenticate
   claude auth
   ```

2. Clone this repository or copy the scripts to your project.

## Usage

### Basic: Single Full Cycle

```bash
# Run complete pipeline: implement -> test -> review -> plan
python claude_orchestra.py --project /path/to/your/project --cycle
```

### Async: Parallel Execution

```bash
# Run with reviewer and planner in parallel
python claude_orchestra_async.py --project /path/to/project --pipeline --parallel
```

### Individual Agents

```bash
# Just implement the next task
python claude_orchestra.py --project /path/to/project --implement

# Test a specific branch
python claude_orchestra.py --project /path/to/project --test --branch feature/new-auth

# Review a specific PR
python claude_orchestra.py --project /path/to/project --review --pr 42

# Run just the planner
python claude_orchestra.py --project /path/to/project --plan
```

### Continuous Mode (Use with caution!)

```bash
# Run up to 5 full cycles automatically
python claude_orchestra.py --project /path/to/project --continuous --max-cycles 5
```

### Create Sample TODO

```bash
python claude_orchestra.py --project /path/to/project --init-todo
```

## Project Requirements

Your project should have:

1. **TODO.md** - Task list for the agents to work from:
   ```markdown
   # TODO

   ## High Priority
   - [ ] Implement user authentication
   - [ ] Add input validation

   ## Medium Priority
   - [ ] Refactor database queries

   ## Completed
   - [x] Initial setup
   ```

2. **Test suite** - So the tester can verify changes
3. **Git repository** - For branching and PRs
4. **GitHub CLI** (`gh`) - For PR creation and review

## Agent Responsibilities

### 🔨 Implementer
- Reads TODO.md for highest priority task
- Creates feature branch
- Implements the feature/fix
- Commits changes
- Outputs: `BRANCH_NAME`, `TASK_COMPLETED`, `FILES_CHANGED`

### 🧪 Tester
- Runs project test suite
- Fixes any failures
- Runs linting/type checks
- Creates PR when tests pass
- Outputs: `PR_NUMBER`, `TEST_RESULTS`, `ISSUES_FIXED`

### 👀 Reviewer
- Reviews PR diff
- Checks for bugs, security, performance
- Approves or requests changes
- Outputs: `REVIEW_DECISION`, `KEY_FEEDBACK`, `ISSUES_FOUND`

### 📋 Planner
- Analyzes codebase health
- Identifies improvement opportunities
- Adds 3-5 new tasks to TODO.md
- Outputs: `TASKS_ADDED`, `RATIONALE`, `CODEBASE_HEALTH`

## Configuration Options

| Option | Default | Description |
|--------|---------|-------------|
| `--model` | sonnet | Claude model (sonnet/opus/haiku) |
| `--timeout` | 600 | Seconds per agent |
| `--max-cycles` | 10 | Max continuous cycles |

## 24/7 Daemon Mode 🌙

For autonomous, long-running development (e.g., while you're on holiday):

```bash
# Start the daemon - runs until stopped
python claude_orchestra_daemon.py --project /path/to/project --daemon

# With limits (recommended for unattended operation)
python claude_orchestra_daemon.py --project /path/to/project --daemon \
    --max-cycles 50 \
    --max-hours 24 \
    --delay 300

# Stop gracefully (from another terminal or cron job)
touch /path/to/project/.claude_orchestra_stop

# View summary of what was done
python claude_orchestra_daemon.py --project /path/to/project --summary

# Reset state to start fresh
python claude_orchestra_daemon.py --project /path/to/project --reset
```

### Daemon Features

| Feature | Description |
|---------|-------------|
| **State Persistence** | Saves progress to `.claude_orchestra_state.json` - can resume after restart |
| **Session Summaries** | Generates `.claude_orchestra_summary.md` with all activity |
| **Graceful Shutdown** | Create `.claude_orchestra_stop` file to stop after current cycle |
| **Auto-Retry Reviews** | Automatically fixes code when reviewer requests changes (up to 3 times) |
| **Cycle Limits** | Set `--max-cycles` and `--max-hours` for safety |

### Example Summary Output

After running, check `.claude_orchestra_summary.md`:

```markdown
# Claude Orchestra Session Summary

## Statistics
| Metric | Value |
|--------|-------|
| Total Cycles | 12 |
| Successful Cycles | 10 |
| PRs Created | 12 |
| PRs Approved | 8 |
| Tasks Added | 35 |

## Recent Cycles
### Cycle 12 ✅
- Task: Add input validation for user endpoints
- Branch: `feature/input-validation`
- PR: #47 (APPROVED)
- Review Iterations: 2
```

### Running on a Schedule (Cron)

For true 24/7 operation, use cron to restart the daemon:

```bash
# Edit crontab
crontab -e

# Run every day at midnight, for 23 hours max
0 0 * * * cd /path/to/project && python claude_orchestra_daemon.py --project . --daemon --max-hours 23 >> /var/log/claude_orchestra.log 2>&1
```

## Safety Considerations

⚠️ **This runs Claude with `--dangerously-skip-permissions`**

This means Claude can:
- Read/write any files in the project
- Execute shell commands
- Make git commits and pushes
- Create PRs

**Recommendations:**
1. Run in a sandboxed environment or VM
2. Use a dedicated test repository first
3. Set reasonable `--max-cycles` limits
4. Review the git history after runs
5. Don't run on production code without review

## Example Output

```
==================================================
Starting async pipeline
==================================================

[1/4] Implementer
[implementer] Starting execution...
[implementer] Completed in 45.2s

[2/4] Tester
[tester] Starting execution...
[tester] Completed in 38.7s

[3+4] Reviewer & Planner (parallel)
[reviewer] Starting execution...
[planner] Starting execution...
[reviewer] Completed in 22.1s
[planner] Completed in 18.4s

==================================================
📊 Pipeline Summary
--------------------------------------------------
✓ IMPLEMENTER: 45.2s
✓ TESTER: 38.7s
✓ REVIEWER: 22.1s
✓ PLANNER: 18.4s
--------------------------------------------------
Total: 124.4s | Success: 4/4
```

## Extending

You can customize agent prompts by editing the `_get_*_prompt()` methods or loading a custom config file with the async version.

## License

MIT
