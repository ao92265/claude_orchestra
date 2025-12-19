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

2. Clone this repository:
   ```bash
   git clone https://github.com/ao92265/claude_orchestra.git
   cd claude_orchestra
   ```

3. Install Python dependencies:
   ```bash
   pip install -r requirements.txt
   ```

## Web Dashboard

Run the web dashboard for a visual interface:

```bash
python dashboard.py
```

Then open **http://localhost:5050** in your browser.

Features:
- Add multiple project tabs before starting
- Real-time log streaming
- Sub-agent tracking
- Activity monitoring

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

## Multi-User Mode 👥

Enable multiple users to run Claude Orchestra on the same repository without conflicts. Uses GitHub Issues as a distributed task queue for coordination.

### How It Works

```
┌─────────────────────────────────────────────────────────────────┐
│  User A (MacBook)         User B (Linux)         User C (Win)  │
│  ┌───────────────┐       ┌───────────────┐      ┌────────────┐ │
│  │ Orchestra     │       │ Orchestra     │      │ Orchestra  │ │
│  │ Agent A       │       │ Agent B       │      │ Agent C    │ │
│  └───────┬───────┘       └───────┬───────┘      └─────┬──────┘ │
│          └───────────────────────┼────────────────────┘        │
│                                  ▼                              │
│                    ┌─────────────────────────┐                 │
│                    │   GitHub Issues         │                 │
│                    │   (Task Queue)          │                 │
│                    │                         │                 │
│                    │ #42 [claimed by A]      │                 │
│                    │ #43 [claimed by B]      │                 │
│                    │ #44 [available]         │                 │
│                    └─────────────────────────┘                 │
└─────────────────────────────────────────────────────────────────┘
```

- **Atomic claiming** via GitHub assignee field
- **Heartbeat system** keeps claims alive (5 min intervals)
- **Stale detection** auto-releases abandoned claims (30 min timeout)
- **Branch naming** `{user}/task/{issue#}` for clear ownership

### Setup (Required for each user)

```bash
# 1. Pull the multi-user branch
git fetch origin
git checkout feature/multi-user-isolation

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set environment variables
export GITHUB_TOKEN=ghp_your_personal_token
export GITHUB_REPO=owner/repo
export ORCHESTRA_MULTI_USER=true

# Optional: Customize timeouts
export ORCHESTRA_CLAIM_TIMEOUT=1800    # 30 min default
export ORCHESTRA_HEARTBEAT_INTERVAL=300 # 5 min default
```

### Getting a GitHub Token

1. Go to https://github.com/settings/tokens
2. Click "Generate new token (classic)"
3. Select scopes: `repo` (full control)
4. Copy the token and set as `GITHUB_TOKEN`

### Initial Sync (Run once per repository)

Before the first run, sync your TODO.md to GitHub Issues:

```bash
python task_coordinator.py sync --repo owner/repo
```

This creates GitHub Issues for each task in TODO.md with labels:
- `orchestra-task` - Identifies managed tasks
- `status:available` - Task can be claimed
- `priority:high/medium/low` - Priority level

### Running with Multi-User Mode

```bash
# Run a single cycle (claims task, implements, creates PR)
python orchestra_multi_user.py --project /path/to/project --cycle

# Run continuous until no tasks remain
python orchestra_multi_user.py --project /path/to/project \
    --continuous --max-cycles 20

# Prefer specific task types
python orchestra_multi_user.py --project /path/to/project \
    --cycle --prefer-priority high --prefer-size small
```

### Monitoring Claims

```bash
# List all active claims
python task_coordinator.py list --repo owner/repo

# Check for stale claims
python task_coordinator.py stale --repo owner/repo

# Release stale claims (auto-releases after timeout anyway)
python task_coordinator.py reclaim --repo owner/repo
```

### CLI Reference

| Command | Description |
|---------|-------------|
| `sync` | Sync TODO.md to GitHub Issues |
| `list` | Show available tasks |
| `claim --issue N` | Claim specific issue |
| `release --issue N` | Release a claim |
| `stale` | List stale claims |
| `reclaim` | Release all stale claims |

### Configuration Options

| Env Variable | Default | Description |
|-------------|---------|-------------|
| `ORCHESTRA_MULTI_USER` | false | Enable multi-user mode |
| `GITHUB_TOKEN` | - | GitHub personal access token |
| `GITHUB_REPO` | - | Repository (owner/repo format) |
| `ORCHESTRA_CLAIM_TIMEOUT` | 1800 | Seconds before claim is stale |
| `ORCHESTRA_HEARTBEAT_INTERVAL` | 300 | Seconds between heartbeats |

### Troubleshooting

**"No tasks available"**
- Check GitHub Issues for `status:available` label
- Run `python task_coordinator.py sync` to sync TODO.md
- Another agent may have claimed all tasks

**"Already assigned"**
- Task was claimed by another agent
- The coordinator will automatically try the next task

**Stale claims not releasing**
- Verify timeout is set correctly
- Run `python task_coordinator.py reclaim` manually

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
