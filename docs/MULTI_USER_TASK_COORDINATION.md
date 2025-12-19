# Multi-User Task Coordination via GitHub Issues

## Overview

This document describes a distributed task coordination system that enables multiple users running Claude Orchestra on different machines to work on the same repository without conflicts. It uses **GitHub Issues as a distributed task queue** with atomic claiming, progress tracking, and automatic cleanup.

---

## Architecture

```
┌──────────────────────────────────────────────────────────────────────────────┐
│                      MULTI-USER TASK COORDINATION                             │
├──────────────────────────────────────────────────────────────────────────────┤
│                                                                               │
│   User A (MacBook)           User B (Linux)           User C (Windows)       │
│   ┌─────────────────┐       ┌─────────────────┐       ┌─────────────────┐   │
│   │ Claude Orchestra│       │ Claude Orchestra│       │ Claude Orchestra│   │
│   │                 │       │                 │       │                 │   │
│   │ TaskCoordinator │       │ TaskCoordinator │       │ TaskCoordinator │   │
│   │  ├─ agent_id    │       │  ├─ agent_id    │       │  ├─ agent_id    │   │
│   │  ├─ claim()     │       │  ├─ claim()     │       │  ├─ claim()     │   │
│   │  └─ complete()  │       │  └─ complete()  │       │  └─ complete()  │   │
│   └────────┬────────┘       └────────┬────────┘       └────────┬────────┘   │
│            │                         │                         │             │
│            └─────────────────────────┼─────────────────────────┘             │
│                                      │                                        │
│                                      ▼                                        │
│                         ┌────────────────────────┐                           │
│                         │      GitHub API        │                           │
│                         │                        │                           │
│                         │  • Issues (Task Queue) │                           │
│                         │  • Labels (State)      │                           │
│                         │  • Assignees (Claims)  │                           │
│                         │  • Comments (History)  │                           │
│                         │  • PRs (Deliverables)  │                           │
│                         └────────────────────────┘                           │
│                                                                               │
└──────────────────────────────────────────────────────────────────────────────┘
```

---

## Data Model

### 1. Agent Identity

Each running instance has a unique identity for tracking and coordination:

```python
@dataclass
class AgentIdentity:
    """Unique identifier for an orchestra agent instance."""
    agent_id: str          # Format: {user}_{hostname}_{timestamp}_{short_hash}
    user: str              # OS username
    hostname: str          # Machine hostname
    started_at: datetime   # When this agent started

    # Example: "aoreilly_macbook_20251219_a3f2"
```

### 2. GitHub Issue Schema

Each task is represented as a GitHub Issue with specific structure:

```yaml
Issue Structure:
  title: "Brief task description"
  body: |
    ## Description
    Detailed task description from TODO.md

    ## Acceptance Criteria
    - [ ] Criterion 1
    - [ ] Criterion 2

    ## Source
    Originally from: TODO.md, line 42
    Task ID: task-{hash}

    ## Claim History
    <!-- Updated by TaskCoordinator - do not edit manually -->
    | Agent ID | Action | Timestamp |
    |----------|--------|-----------|

  labels:
    - "orchestra-task"           # Identifies as managed task
    - "priority:{high|medium|low}"
    - "size:{small|medium|large}"
    - "status:{available|claimed|in-progress|blocked|review}"

  assignee: null | github_username   # Who claimed it
```

### 3. Task Claim Record

When an agent claims a task, it adds a structured comment:

```markdown
## 🤖 Task Claimed

| Field | Value |
|-------|-------|
| Agent ID | `aoreilly_macbook_20251219_a3f2` |
| Instance | aoreilly@macbook.local (PID: 12345) |
| Claimed At | 2025-12-19T15:30:00Z |
| Branch | `task/123-add-user-auth` |
| Heartbeat | 2025-12-19T15:30:00Z |

---
_This claim will expire if no heartbeat for 30 minutes._
```

### 4. Heartbeat Updates

While working, agent updates claim comment periodically:

```markdown
## 🤖 Task Claimed

| Field | Value |
|-------|-------|
| Agent ID | `aoreilly_macbook_20251219_a3f2` |
| Instance | aoreilly@macbook.local (PID: 12345) |
| Claimed At | 2025-12-19T15:30:00Z |
| Branch | `task/123-add-user-auth` |
| Heartbeat | 2025-12-19T15:45:00Z |
| Progress | 3 files modified, 2 tests passing |

---
_This claim will expire if no heartbeat for 30 minutes._
```

---

## Label System

### Required Labels (Auto-Created)

```python
ORCHESTRA_LABELS = {
    # Task identification
    "orchestra-task": {
        "color": "5319e7",
        "description": "Task managed by Claude Orchestra"
    },

    # Status labels (mutually exclusive)
    "status:available": {
        "color": "0e8a16",
        "description": "Task is available for claiming"
    },
    "status:claimed": {
        "color": "fbca04",
        "description": "Task has been claimed by an agent"
    },
    "status:in-progress": {
        "color": "1d76db",
        "description": "Agent is actively working on this task"
    },
    "status:blocked": {
        "color": "d93f0b",
        "description": "Task is blocked, needs intervention"
    },
    "status:review": {
        "color": "c5def5",
        "description": "PR created, awaiting review"
    },

    # Priority labels
    "priority:high": {"color": "b60205", "description": "High priority task"},
    "priority:medium": {"color": "fbca04", "description": "Medium priority task"},
    "priority:low": {"color": "0e8a16", "description": "Low priority task"},

    # Size labels
    "size:small": {"color": "c2e0c6", "description": "Small task (~30 min)"},
    "size:medium": {"color": "fef2c0", "description": "Medium task (~2 hours)"},
    "size:large": {"color": "f9d0c4", "description": "Large task (~1 day)"},
}
```

---

## TaskCoordinator Class API

```python
class TaskCoordinator:
    """
    Coordinates task assignment across multiple orchestra instances.
    Uses GitHub Issues as a distributed task queue.
    """

    def __init__(
        self,
        repo_owner: str,
        repo_name: str,
        github_token: str,
        instance_manager: InstanceManager,
        heartbeat_interval: int = 300,  # 5 minutes
        claim_timeout: int = 1800,       # 30 minutes
    ):
        self.agent_id = self._generate_agent_id(instance_manager)
        self.github = GitHubClient(repo_owner, repo_name, github_token)
        ...

    # ─────────────────────────────────────────────────────────────
    # Setup & Sync
    # ─────────────────────────────────────────────────────────────

    async def setup(self) -> None:
        """Initialize labels and sync TODO.md to issues."""
        await self._ensure_labels_exist()
        await self.sync_todos_to_issues()

    async def sync_todos_to_issues(
        self,
        todo_files: List[str] = ["TODO.md", "docs/TODO.md"]
    ) -> SyncResult:
        """
        Sync tasks from TODO.md files to GitHub Issues.

        - Creates issues for new tasks
        - Updates existing issues if task description changed
        - Does NOT delete issues (tasks may be intentionally kept)

        Returns:
            SyncResult with created, updated, unchanged counts
        """

    # ─────────────────────────────────────────────────────────────
    # Task Discovery
    # ─────────────────────────────────────────────────────────────

    async def get_available_tasks(
        self,
        priority: Optional[str] = None,
        size: Optional[str] = None,
        limit: int = 10
    ) -> List[Task]:
        """
        Get tasks available for claiming.

        Filters:
            - Has label: orchestra-task
            - Has label: status:available
            - No assignee
            - Optionally filter by priority/size

        Returns:
            List of Task objects, sorted by priority
        """

    async def get_my_claimed_tasks(self) -> List[Task]:
        """Get tasks currently claimed by this agent."""

    async def get_all_active_claims(self) -> List[ClaimInfo]:
        """Get all active claims across all agents (for visibility)."""

    # ─────────────────────────────────────────────────────────────
    # Claiming
    # ─────────────────────────────────────────────────────────────

    async def claim_task(
        self,
        issue_number: int,
        branch_name: Optional[str] = None
    ) -> ClaimResult:
        """
        Attempt to claim a task.

        Atomicity:
            1. Check current state (available, no assignee)
            2. Assign to self
            3. Update labels: remove status:available, add status:claimed
            4. Add claim comment with agent metadata

        Race Handling:
            If another agent claimed it first (409 Conflict or state mismatch),
            returns ClaimResult(success=False, reason="already_claimed")

        Returns:
            ClaimResult with success status and claim details
        """

    async def claim_next_available(
        self,
        priority: Optional[str] = None,
        size: Optional[str] = None
    ) -> ClaimResult:
        """
        Claim the next available task matching criteria.

        Iterates through available tasks and attempts to claim each
        until one succeeds or no tasks remain.

        Returns:
            ClaimResult for the claimed task, or failure if none available
        """

    async def release_claim(
        self,
        issue_number: int,
        reason: str = "released"
    ) -> None:
        """
        Release a claim without completing the task.

        - Removes assignee
        - Updates labels: status:claimed → status:available
        - Adds comment explaining release
        """

    # ─────────────────────────────────────────────────────────────
    # Progress Tracking
    # ─────────────────────────────────────────────────────────────

    async def update_progress(
        self,
        issue_number: int,
        status: str = "in-progress",
        progress_note: Optional[str] = None
    ) -> None:
        """
        Update task progress and heartbeat.

        - Updates claim comment with new heartbeat timestamp
        - Optionally updates status label
        - Optionally adds progress note
        """

    async def start_heartbeat_loop(self) -> asyncio.Task:
        """
        Start background task that updates heartbeats for all claimed tasks.

        Runs every heartbeat_interval seconds until stopped.
        """

    async def stop_heartbeat_loop(self) -> None:
        """Stop the heartbeat loop."""

    # ─────────────────────────────────────────────────────────────
    # Completion
    # ─────────────────────────────────────────────────────────────

    async def mark_pr_created(
        self,
        issue_number: int,
        pr_number: int
    ) -> None:
        """
        Mark that a PR was created for this task.

        - Updates label: status:in-progress → status:review
        - Links PR to issue
        - Adds comment with PR link
        """

    async def complete_task(
        self,
        issue_number: int,
        pr_number: Optional[int] = None
    ) -> None:
        """
        Mark task as complete.

        - Closes issue (or lets PR auto-close via "Fixes #N")
        - Adds completion comment with summary
        - Removes from active claims
        """

    async def mark_blocked(
        self,
        issue_number: int,
        reason: str
    ) -> None:
        """
        Mark task as blocked.

        - Updates label: → status:blocked
        - Adds comment explaining blockage
        - Releases assignment so human can investigate
        """

    # ─────────────────────────────────────────────────────────────
    # Stale Claim Management
    # ─────────────────────────────────────────────────────────────

    async def check_stale_claims(self) -> List[StaleClaimInfo]:
        """
        Find claims that have gone stale (no heartbeat within timeout).

        Returns:
            List of stale claims with agent info and last heartbeat
        """

    async def reclaim_stale_tasks(self) -> int:
        """
        Release claims that have gone stale.

        - Checks all claimed tasks for stale heartbeats
        - Releases stale claims
        - Adds comment explaining automatic release

        Returns:
            Number of stale claims released
        """
```

---

## Workflow Diagrams

### 1. Agent Startup

```
┌─────────────────────────────────────────────────────────────────┐
│                     AGENT STARTUP FLOW                           │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. Agent Starts                                                 │
│     │                                                            │
│     ▼                                                            │
│  2. InstanceManager.register()                                   │
│     ├─ Generate agent_id                                         │
│     ├─ Allocate dashboard port                                   │
│     └─ Register in local instance registry                       │
│     │                                                            │
│     ▼                                                            │
│  3. TaskCoordinator.setup()                                      │
│     ├─ Verify GitHub API access                                  │
│     ├─ Ensure required labels exist                              │
│     └─ Optionally sync TODO.md → Issues                          │
│     │                                                            │
│     ▼                                                            │
│  4. TaskCoordinator.reclaim_stale_tasks()                        │
│     └─ Release any orphaned claims from crashed agents           │
│     │                                                            │
│     ▼                                                            │
│  5. TaskCoordinator.start_heartbeat_loop()                       │
│     └─ Background task to keep claims alive                      │
│     │                                                            │
│     ▼                                                            │
│  6. Ready to process tasks                                       │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 2. Task Claiming Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                    TASK CLAIMING FLOW                            │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. get_available_tasks(priority="high")                         │
│     │                                                            │
│     ▼                                                            │
│  2. [Issue #42, Issue #38, Issue #55]  ← sorted by priority      │
│     │                                                            │
│     ▼                                                            │
│  3. claim_task(issue_number=42)                                  │
│     │                                                            │
│     ├─ GET /issues/42                                            │
│     │   └─ Check: assignee == null?                              │
│     │   └─ Check: has label status:available?                    │
│     │                                                            │
│     ├─ If checks pass:                                           │
│     │   │                                                        │
│     │   ├─ PATCH /issues/42 {assignee: "my-username"}            │
│     │   │                                                        │
│     │   ├─ POST /issues/42/labels                                │
│     │   │   └─ Remove: status:available                          │
│     │   │   └─ Add: status:claimed                               │
│     │   │                                                        │
│     │   ├─ POST /issues/42/comments                              │
│     │   │   └─ Add claim comment with agent metadata             │
│     │   │                                                        │
│     │   └─ Return ClaimResult(success=True, issue=42, ...)       │
│     │                                                            │
│     └─ If claim fails (race condition):                          │
│         │                                                        │
│         └─ Try next: claim_task(issue_number=38)                 │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 3. Work + Completion Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                   WORK & COMPLETION FLOW                         │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  1. Claimed Issue #42                                            │
│     │                                                            │
│     ▼                                                            │
│  2. Create branch: task/42-add-user-auth                         │
│     │                                                            │
│     ▼                                                            │
│  3. update_progress(42, "in-progress")                           │
│     └─ Label: status:claimed → status:in-progress                │
│     │                                                            │
│     ▼                                                            │
│  4. [Implementer Agent Works]                                    │
│     │                                                            │
│     │  ┌──────────────────────────────────────────────┐          │
│     │  │ Meanwhile: heartbeat_loop runs every 5min    │          │
│     │  │ └─ update_progress(42) ← refreshes heartbeat │          │
│     │  └──────────────────────────────────────────────┘          │
│     │                                                            │
│     ▼                                                            │
│  5. Work complete → Create PR #99                                │
│     │                                                            │
│     ▼                                                            │
│  6. mark_pr_created(42, pr_number=99)                            │
│     ├─ Label: status:in-progress → status:review                 │
│     └─ Comment: "PR #99 created for this task"                   │
│     │                                                            │
│     ▼                                                            │
│  7. [Reviewer Agent Reviews]                                     │
│     │                                                            │
│     ├─ Approved → PR merged → Issue auto-closes                  │
│     │                                                            │
│     └─ Changes requested → back to step 4                        │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

### 4. Stale Claim Recovery

```
┌─────────────────────────────────────────────────────────────────┐
│                 STALE CLAIM RECOVERY FLOW                        │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  Agent B starts (or runs periodic check)                         │
│     │                                                            │
│     ▼                                                            │
│  check_stale_claims()                                            │
│     │                                                            │
│     ├─ Query: issues with status:claimed OR status:in-progress   │
│     │                                                            │
│     ├─ For each claimed issue:                                   │
│     │   ├─ Parse claim comment for last heartbeat                │
│     │   ├─ If (now - heartbeat) > claim_timeout:                 │
│     │   │   └─ Add to stale_claims list                          │
│     │   └─ Else: claim is still active                           │
│     │                                                            │
│     ▼                                                            │
│  reclaim_stale_tasks()                                           │
│     │                                                            │
│     ├─ For each stale claim:                                     │
│     │   ├─ Remove assignee                                       │
│     │   ├─ Update label: → status:available                      │
│     │   └─ Add comment:                                          │
│     │       "⚠️ Claim released automatically.                    │
│     │        Agent aoreilly_macbook_... stopped responding       │
│     │        at 2025-12-19T14:30:00Z (30+ min ago).              │
│     │        Task is now available for claiming."                │
│     │                                                            │
│     └─ Return count of released claims                           │
│                                                                  │
└─────────────────────────────────────────────────────────────────┘
```

---

## Integration with Existing Orchestra

### Modified Implementer Flow

```python
# In ClaudeOrchestra.run_implementer()

async def run_implementer(self, task_description: Optional[str] = None) -> AgentResult:
    """Run the Implementer agent with coordinated task claiming."""

    # If multi-user mode is enabled
    if self.task_coordinator:
        # 1. Claim a task
        claim_result = await self.task_coordinator.claim_next_available(
            priority="high" if self.task_mode == "large" else None,
            size=self.task_mode if self.task_mode != "normal" else None
        )

        if not claim_result.success:
            logger.info("No available tasks to claim")
            return AgentResult(
                agent=AgentRole.IMPLEMENTER,
                success=False,
                message="No tasks available"
            )

        # 2. Use the claimed task as the task description
        task_description = claim_result.task.description
        self.current_issue = claim_result.issue_number

        # 3. Create branch named after issue
        branch_name = f"task/{claim_result.issue_number}-{self._slugify(claim_result.task.title)}"

        # 4. Update progress
        await self.task_coordinator.update_progress(
            claim_result.issue_number,
            status="in-progress"
        )

    # ... existing implementer logic ...

    # After successful implementation
    if self.task_coordinator and self.current_issue:
        if pr_number:
            await self.task_coordinator.mark_pr_created(
                self.current_issue,
                pr_number
            )
```

### Configuration

```python
# orchestra_config.py

@dataclass
class MultiUserConfig:
    """Configuration for multi-user task coordination."""
    enabled: bool = False
    github_token: str = ""  # Or from env: GITHUB_TOKEN
    repo_owner: str = ""
    repo_name: str = ""
    heartbeat_interval: int = 300  # 5 minutes
    claim_timeout: int = 1800      # 30 minutes
    auto_sync_todos: bool = True   # Sync TODO.md to issues on startup

    @classmethod
    def from_env(cls) -> 'MultiUserConfig':
        return cls(
            enabled=os.getenv("ORCHESTRA_MULTI_USER", "false").lower() == "true",
            github_token=os.getenv("GITHUB_TOKEN", ""),
            repo_owner=os.getenv("GITHUB_REPO_OWNER", ""),
            repo_name=os.getenv("GITHUB_REPO_NAME", ""),
        )
```

### CLI Options

```bash
# Enable multi-user mode
python claude_orchestra_daemon.py --multi-user

# With explicit config
python claude_orchestra_daemon.py \
    --multi-user \
    --github-token $GITHUB_TOKEN \
    --repo aoreilly/claude_orchestra

# Sync TODO.md to issues (one-time)
python claude_orchestra.py sync-todos

# Check active claims
python claude_orchestra.py list-claims

# Release stale claims
python claude_orchestra.py release-stale
```

---

## GitHub API Usage

### Rate Limit Considerations

| Operation | API Calls | Frequency |
|-----------|-----------|-----------|
| List available tasks | 1 | Per claim attempt |
| Claim task | 3-4 | Per task claimed |
| Heartbeat update | 1 | Every 5 minutes |
| Complete task | 2 | Per task completed |
| Stale check | 1-2 | Every 15 minutes |

**Estimated usage per hour per agent:** 15-25 API calls
**GitHub rate limit:** 5000/hour (authenticated)

This is well within limits even with 10+ concurrent agents.

### Required GitHub Permissions

```yaml
Repository Permissions:
  issues: write      # Create, update, close issues
  pull_requests: read # Link PRs to issues

# If using GitHub App:
permissions:
  issues: write
  pull_requests: read
  metadata: read
```

---

## Error Handling

### Race Condition Handling

```python
async def claim_task(self, issue_number: int) -> ClaimResult:
    try:
        # Check state
        issue = await self.github.get_issue(issue_number)

        if issue.assignee is not None:
            return ClaimResult(success=False, reason="already_assigned")

        if "status:available" not in issue.labels:
            return ClaimResult(success=False, reason="not_available")

        # Attempt atomic claim
        await self.github.update_issue(
            issue_number,
            assignee=self.github_username,
        )

        # Verify we got it (another agent might have claimed simultaneously)
        issue = await self.github.get_issue(issue_number)
        if issue.assignee != self.github_username:
            return ClaimResult(success=False, reason="race_condition")

        # Update labels and add comment
        await self._finalize_claim(issue_number)

        return ClaimResult(success=True, issue_number=issue_number)

    except GitHubConflictError:
        return ClaimResult(success=False, reason="conflict")
```

### Network Failure Handling

```python
async def heartbeat_with_retry(self, issue_number: int, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            await self.update_progress(issue_number)
            return
        except NetworkError:
            if attempt < max_retries - 1:
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
            else:
                logger.warning(f"Failed to update heartbeat for #{issue_number}")
                # Continue working - claim won't expire for claim_timeout
```

---

## Dashboard Integration

### New Socket Events

```python
# In dashboard.py

@socketio.on('get_active_claims')
def handle_get_claims():
    """Get all active task claims across agents."""
    if task_coordinator:
        claims = await task_coordinator.get_all_active_claims()
        emit('claims_update', {
            'claims': [c.to_dict() for c in claims],
            'my_agent_id': task_coordinator.agent_id
        })

@socketio.on('release_claim')
def handle_release_claim(data):
    """Manually release a claim."""
    issue_number = data.get('issue_number')
    if task_coordinator:
        await task_coordinator.release_claim(issue_number, reason="manual_release")
        emit('claim_released', {'issue_number': issue_number})
```

### UI Components

```
┌─────────────────────────────────────────────────────────────────┐
│  ACTIVE CLAIMS                                           [↻]    │
├─────────────────────────────────────────────────────────────────┤
│                                                                  │
│  #42 Add user authentication                                     │
│  ├─ Claimed by: aoreilly_macbook_20251219_a3f2 (YOU)            │
│  ├─ Status: in-progress                                          │
│  ├─ Branch: task/42-add-user-auth                               │
│  ├─ Heartbeat: 2 min ago ●                                       │
│  └─ [Release Claim]                                              │
│                                                                  │
│  #38 Fix pagination bug                                          │
│  ├─ Claimed by: bob_linux_20251219_b7c3                         │
│  ├─ Status: claimed                                              │
│  ├─ Branch: task/38-fix-pagination                              │
│  └─ Heartbeat: 8 min ago ●                                       │
│                                                                  │
│  #55 Update documentation                                        │
│  ├─ Claimed by: charlie_windows_20251219_d9e4                   │
│  ├─ Status: review (PR #101)                                     │
│  └─ Heartbeat: 15 min ago ◐                                      │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│  AVAILABLE TASKS                                          [5]   │
│  #60 Add rate limiting  [high] [medium]                         │
│  #61 Improve error messages  [medium] [small]                   │
│  #62 Add unit tests for auth  [medium] [small]                  │
│  ...                                                             │
└─────────────────────────────────────────────────────────────────┘
```

---

## Implementation Plan

### Phase 1: Core Infrastructure
1. Create `task_coordinator.py` with TaskCoordinator class
2. Implement GitHub API client wrapper
3. Add AgentIdentity generation
4. Implement label management

### Phase 2: Task Claiming
5. Implement get_available_tasks()
6. Implement claim_task() with race handling
7. Implement release_claim()
8. Add TODO.md → Issues sync

### Phase 3: Progress Tracking
9. Implement heartbeat system
10. Implement update_progress()
11. Implement stale claim detection
12. Implement reclaim_stale_tasks()

### Phase 4: Completion Flow
13. Implement mark_pr_created()
14. Implement complete_task()
15. Implement mark_blocked()

### Phase 5: Integration
16. Integrate with ClaudeOrchestra
17. Add CLI commands
18. Add dashboard UI
19. Add configuration options

### Phase 6: Testing & Polish
20. Unit tests for TaskCoordinator
21. Integration tests with GitHub API
22. Documentation
23. Example workflows

---

## Design Decisions

1. **GitHub authentication**: Each user uses their personal GitHub token
   - Natural ownership of claims
   - Commits attributed to actual user
   - No shared bot account needed

2. **TODO.md sync**: Auto-sync on startup
   - Keeps issues in sync with TODO.md
   - New tasks automatically become available
   - Reduces manual overhead

3. **Branch naming**: `{user}/task/{issue#}` format
   - Example: `aoreilly/task/42`
   - Clear ownership visible in branch name
   - Avoids slug collision issues

4. **Stale claim timeout**: Configurable per-project
   - Default: 30 minutes
   - Configurable via `ORCHESTRA_CLAIM_TIMEOUT` env var
   - Or `--claim-timeout` CLI flag
