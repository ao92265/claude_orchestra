#!/usr/bin/env python3
"""
Task Coordinator - Multi-user task coordination via GitHub Issues

Provides distributed task claiming, progress tracking, and stale claim recovery
to enable multiple users running Claude Orchestra on different machines to work
on the same repository without conflicts.
"""

import os
import re
import ssl
import asyncio
import hashlib
import logging
from pathlib import Path
from datetime import datetime, timezone
from typing import Optional, List, Dict, Any, Tuple
from dataclasses import dataclass, field, asdict
from enum import Enum
import aiohttp
import socket

# Try to use certifi for SSL certificates (needed on macOS)
try:
    import certifi
    SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    # Fall back to default SSL context
    SSL_CONTEXT = ssl.create_default_context()

logger = logging.getLogger(__name__)


# =============================================================================
# Data Models
# =============================================================================

class TaskStatus(Enum):
    """Status of a task in the coordination system."""
    AVAILABLE = "available"
    CLAIMED = "claimed"
    IN_PROGRESS = "in-progress"
    BLOCKED = "blocked"
    REVIEW = "review"


class TaskPriority(Enum):
    """Priority level for tasks."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class TaskSize(Enum):
    """Estimated size/complexity of tasks."""
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"


@dataclass
class AgentIdentity:
    """Unique identifier for an orchestra agent instance."""
    agent_id: str
    user: str
    hostname: str
    pid: int
    started_at: str
    github_username: str = ""

    @classmethod
    def generate(cls, github_username: str = "") -> 'AgentIdentity':
        """Generate a new unique agent identity."""
        user = os.getenv("USER") or os.getenv("USERNAME") or "unknown"
        hostname = socket.gethostname()
        pid = os.getpid()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Short hash for uniqueness
        hash_input = f"{user}_{hostname}_{pid}_{timestamp}"
        short_hash = hashlib.sha256(hash_input.encode()).hexdigest()[:4]

        agent_id = f"{user}_{hostname}_{timestamp}_{short_hash}"

        return cls(
            agent_id=agent_id,
            user=user,
            hostname=hostname,
            pid=pid,
            started_at=datetime.now(timezone.utc).isoformat(),
            github_username=github_username
        )

    def to_dict(self) -> Dict:
        return asdict(self)


@dataclass
class Task:
    """A task from GitHub Issues."""
    issue_number: int
    title: str
    description: str
    status: TaskStatus
    priority: Optional[TaskPriority] = None
    size: Optional[TaskSize] = None
    assignee: Optional[str] = None
    labels: List[str] = field(default_factory=list)
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    source_file: Optional[str] = None  # Original TODO.md location
    task_id: Optional[str] = None  # Hash-based ID for deduplication


@dataclass
class ClaimInfo:
    """Information about a task claim."""
    issue_number: int
    agent_id: str
    github_username: str
    claimed_at: str
    last_heartbeat: str
    branch_name: Optional[str] = None
    progress_note: Optional[str] = None


@dataclass
class ClaimResult:
    """Result of a claim attempt."""
    success: bool
    issue_number: Optional[int] = None
    task: Optional[Task] = None
    branch_name: Optional[str] = None
    reason: Optional[str] = None
    claim_info: Optional[ClaimInfo] = None


@dataclass
class SyncResult:
    """Result of syncing TODO.md to GitHub Issues."""
    created: int = 0
    updated: int = 0
    unchanged: int = 0
    errors: List[str] = field(default_factory=list)


# =============================================================================
# Label Definitions
# =============================================================================

ORCHESTRA_LABELS = {
    # Task identification
    "orchestra-task": {
        "color": "5319e7",
        "description": "Task managed by Claude Orchestra"
    },
    # Status labels
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
    "priority:high": {
        "color": "b60205",
        "description": "High priority task"
    },
    "priority:medium": {
        "color": "fbca04",
        "description": "Medium priority task"
    },
    "priority:low": {
        "color": "0e8a16",
        "description": "Low priority task"
    },
    # Size labels
    "size:small": {
        "color": "c2e0c6",
        "description": "Small task (~30 min)"
    },
    "size:medium": {
        "color": "fef2c0",
        "description": "Medium task (~2 hours)"
    },
    "size:large": {
        "color": "f9d0c4",
        "description": "Large task (~1 day)"
    },
}


# =============================================================================
# GitHub API Client
# =============================================================================

class GitHubAPIError(Exception):
    """Error from GitHub API."""
    def __init__(self, message: str, status_code: int = 0, response: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.response = response


class GitHubClient:
    """Async GitHub API client for issue management."""

    def __init__(self, owner: str, repo: str, token: str):
        self.owner = owner
        self.repo = repo
        self.token = token
        self.base_url = "https://api.github.com"
        self._session: Optional[aiohttp.ClientSession] = None
        self._username: Optional[str] = None

    async def _get_session(self) -> aiohttp.ClientSession:
        """Get or create aiohttp session."""
        if self._session is None or self._session.closed:
            # Use TCPConnector with SSL context to fix macOS certificate issues
            connector = aiohttp.TCPConnector(ssl=SSL_CONTEXT)
            self._session = aiohttp.ClientSession(
                connector=connector,
                headers={
                    "Authorization": f"token {self.token}",
                    "Accept": "application/vnd.github.v3+json",
                    "User-Agent": "Claude-Orchestra-TaskCoordinator"
                }
            )
        return self._session

    async def close(self):
        """Close the HTTP session."""
        if self._session and not self._session.closed:
            await self._session.close()

    async def get_authenticated_user(self) -> str:
        """Get the username of the authenticated user."""
        if self._username:
            return self._username

        session = await self._get_session()
        async with session.get(f"{self.base_url}/user") as resp:
            if resp.status != 200:
                raise GitHubAPIError("Failed to get authenticated user", resp.status)
            data = await resp.json()
            self._username = data["login"]
            return self._username

    async def _request(
        self,
        method: str,
        endpoint: str,
        data: Optional[Dict] = None,
        params: Optional[Dict] = None
    ) -> Tuple[int, Any]:
        """Make an API request."""
        session = await self._get_session()
        url = f"{self.base_url}{endpoint}"

        async with session.request(method, url, json=data, params=params) as resp:
            try:
                response_data = await resp.json()
            except:
                response_data = await resp.text()

            return resp.status, response_data

    # -------------------------------------------------------------------------
    # Repository
    # -------------------------------------------------------------------------

    async def verify_repo_access(self) -> Dict:
        """Verify the token has access to the repository."""
        status, data = await self._request(
            "GET",
            f"/repos/{self.owner}/{self.repo}"
        )
        if status == 404:
            raise GitHubAPIError(
                f"Repository '{self.owner}/{self.repo}' not found or not accessible. "
                f"Check that the repo exists and your token has 'repo' scope.",
                status
            )
        if status != 200:
            raise GitHubAPIError(f"Failed to access repository: {data}", status)
        return data

    # -------------------------------------------------------------------------
    # Labels
    # -------------------------------------------------------------------------

    async def get_labels(self) -> List[Dict]:
        """Get all labels in the repository."""
        status, data = await self._request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/labels",
            params={"per_page": 100}
        )
        if status == 404:
            raise GitHubAPIError(
                f"Cannot access labels for '{self.owner}/{self.repo}'. "
                f"Repository not found or token lacks 'repo' scope.",
                status
            )
        if status != 200:
            raise GitHubAPIError(f"Failed to get labels: {data}", status)
        return data

    async def create_label(self, name: str, color: str, description: str = "") -> Dict:
        """Create a new label."""
        status, data = await self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/labels",
            data={"name": name, "color": color, "description": description}
        )
        if status not in (200, 201):
            if status == 422:  # Already exists
                return {"name": name}
            raise GitHubAPIError(f"Failed to create label: {data}", status)
        return data

    async def ensure_labels_exist(self) -> None:
        """Ensure all required orchestra labels exist."""
        existing = await self.get_labels()
        existing_names = {label["name"] for label in existing}

        for name, config in ORCHESTRA_LABELS.items():
            if name not in existing_names:
                logger.info(f"Creating label: {name}")
                await self.create_label(
                    name=name,
                    color=config["color"],
                    description=config["description"]
                )

    # -------------------------------------------------------------------------
    # Issues
    # -------------------------------------------------------------------------

    async def get_issue(self, issue_number: int) -> Dict:
        """Get a single issue."""
        status, data = await self._request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}"
        )
        if status != 200:
            raise GitHubAPIError(f"Failed to get issue #{issue_number}: {data}", status)
        return data

    async def list_issues(
        self,
        labels: Optional[List[str]] = None,
        state: str = "open",
        assignee: Optional[str] = None,
        per_page: int = 100,
        paginate_all: bool = False
    ) -> List[Dict]:
        """List issues with optional filters.

        Args:
            paginate_all: If True, fetches ALL pages of results. Default False for backwards compat.
        """
        params = {
            "state": state,
            "per_page": per_page
        }
        if labels:
            params["labels"] = ",".join(labels)
        if assignee is not None:
            params["assignee"] = assignee if assignee else "none"

        all_issues = []
        page = 1

        while True:
            params["page"] = page
            status, data = await self._request(
                "GET",
                f"/repos/{self.owner}/{self.repo}/issues",
                params=params
            )
            if status != 200:
                raise GitHubAPIError(f"Failed to list issues: {data}", status)

            all_issues.extend(data)

            # If not paginating all, or got fewer results than per_page, we're done
            if not paginate_all or len(data) < per_page:
                break

            page += 1
            # Safety limit to prevent infinite loops
            if page > 50:
                logger.warning("Hit pagination limit (5000 issues)")
                break

        return all_issues

    async def create_issue(
        self,
        title: str,
        body: str,
        labels: Optional[List[str]] = None
    ) -> Dict:
        """Create a new issue."""
        data = {"title": title, "body": body}
        if labels:
            data["labels"] = labels

        status, response = await self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/issues",
            data=data
        )
        if status not in (200, 201):
            raise GitHubAPIError(f"Failed to create issue: {response}", status)
        return response

    async def update_issue(
        self,
        issue_number: int,
        title: Optional[str] = None,
        body: Optional[str] = None,
        state: Optional[str] = None,
        assignee: Optional[str] = None,
        labels: Optional[List[str]] = None
    ) -> Dict:
        """Update an issue."""
        data = {}
        if title is not None:
            data["title"] = title
        if body is not None:
            data["body"] = body
        if state is not None:
            data["state"] = state
        if assignee is not None:
            # Empty string means unassign
            data["assignee"] = assignee if assignee else None
        if labels is not None:
            data["labels"] = labels

        status, response = await self._request(
            "PATCH",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}",
            data=data
        )
        if status != 200:
            raise GitHubAPIError(f"Failed to update issue #{issue_number}: {response}", status)
        return response

    async def add_labels(self, issue_number: int, labels: List[str]) -> List[Dict]:
        """Add labels to an issue."""
        status, data = await self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/labels",
            data={"labels": labels}
        )
        if status != 200:
            raise GitHubAPIError(f"Failed to add labels: {data}", status)
        return data

    async def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue."""
        status, data = await self._request(
            "DELETE",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/labels/{label}"
        )
        # 404 is fine - label wasn't there
        if status not in (200, 204, 404):
            raise GitHubAPIError(f"Failed to remove label: {data}", status)

    # -------------------------------------------------------------------------
    # Comments
    # -------------------------------------------------------------------------

    async def get_comments(self, issue_number: int) -> List[Dict]:
        """Get all comments on an issue."""
        status, data = await self._request(
            "GET",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
            params={"per_page": 100}
        )
        if status != 200:
            raise GitHubAPIError(f"Failed to get comments: {data}", status)
        return data

    async def create_comment(self, issue_number: int, body: str) -> Dict:
        """Create a comment on an issue."""
        status, data = await self._request(
            "POST",
            f"/repos/{self.owner}/{self.repo}/issues/{issue_number}/comments",
            data={"body": body}
        )
        if status not in (200, 201):
            raise GitHubAPIError(f"Failed to create comment: {data}", status)
        return data

    async def update_comment(self, comment_id: int, body: str) -> Dict:
        """Update an existing comment."""
        status, data = await self._request(
            "PATCH",
            f"/repos/{self.owner}/{self.repo}/issues/comments/{comment_id}",
            data={"body": body}
        )
        if status != 200:
            raise GitHubAPIError(f"Failed to update comment: {data}", status)
        return data

    async def search_issues(self, query: str) -> List[Dict]:
        """Search issues."""
        full_query = f"repo:{self.owner}/{self.repo} {query}"
        status, data = await self._request(
            "GET",
            "/search/issues",
            params={"q": full_query, "per_page": 100}
        )
        if status != 200:
            raise GitHubAPIError(f"Failed to search issues: {data}", status)
        return data.get("items", [])


# =============================================================================
# Task Coordinator
# =============================================================================

class TaskCoordinator:
    """
    Coordinates task assignment across multiple orchestra instances.
    Uses GitHub Issues as a distributed task queue.
    """

    CLAIM_COMMENT_MARKER = "<!-- orchestra-claim -->"

    def __init__(
        self,
        repo_owner: str,
        repo_name: str,
        github_token: str,
        project_path: Optional[str] = None,
        heartbeat_interval: int = 300,  # 5 minutes
        claim_timeout: int = 1800,       # 30 minutes
    ):
        self.repo_owner = repo_owner
        self.repo_name = repo_name
        self.project_path = Path(project_path) if project_path else Path.cwd()
        self.heartbeat_interval = heartbeat_interval
        self.claim_timeout = claim_timeout

        self.github = GitHubClient(repo_owner, repo_name, github_token)
        self.agent: Optional[AgentIdentity] = None

        self._heartbeat_task: Optional[asyncio.Task] = None
        self._claimed_issues: Dict[int, ClaimInfo] = {}
        self._claim_comment_ids: Dict[int, int] = {}  # issue_number -> comment_id

    async def setup(self) -> None:
        """Initialize the coordinator."""
        # Get GitHub username and create agent identity
        github_username = await self.github.get_authenticated_user()
        self.agent = AgentIdentity.generate(github_username=github_username)

        logger.info(f"TaskCoordinator initialized")
        logger.info(f"  Agent ID: {self.agent.agent_id}")
        logger.info(f"  GitHub User: {github_username}")
        logger.info(f"  Repo: {self.repo_owner}/{self.repo_name}")

        # Verify repo access first
        await self.github.verify_repo_access()

        # Ensure labels exist
        await self.github.ensure_labels_exist()

    async def close(self) -> None:
        """Cleanup resources."""
        await self.stop_heartbeat_loop()
        await self.github.close()

    # -------------------------------------------------------------------------
    # TODO.md Sync
    # -------------------------------------------------------------------------

    async def sync_todos_to_issues(
        self,
        todo_files: Optional[List[str]] = None
    ) -> SyncResult:
        """
        Sync tasks from TODO.md files to GitHub Issues.

        Creates issues for new tasks, updates existing if description changed.
        Does NOT delete issues (tasks may be intentionally kept).
        """
        if todo_files is None:
            todo_files = ["TODO.md", "docs/TODO.md", "docs/TASKS.md"]

        result = SyncResult()
        tasks = []

        # Parse all TODO files
        for todo_file in todo_files:
            file_path = self.project_path / todo_file
            if file_path.exists():
                parsed = self._parse_todo_file(file_path)
                tasks.extend(parsed)
                logger.info(f"Parsed {len(parsed)} tasks from {todo_file}")

        if not tasks:
            logger.info("No tasks found in TODO files")
            return result

        # Get existing orchestra issues (fetch ALL pages to avoid duplicates)
        existing_issues = await self.github.list_issues(
            labels=["orchestra-task"],
            state="all",
            per_page=100,
            paginate_all=True
        )
        logger.info(f"Found {len(existing_issues)} existing orchestra issues")

        # Build map of task_id to issue
        existing_by_task_id = {}
        for issue in existing_issues:
            task_id = self._extract_task_id_from_body(issue.get("body", ""))
            if task_id:
                existing_by_task_id[task_id] = issue

        # Sync each task
        total_tasks = len(tasks)
        for idx, (task_title, task_body, task_id, source_file, priority) in enumerate(tasks):
            try:
                if task_id in existing_by_task_id:
                    # Skip - already exists with same title
                    existing = existing_by_task_id[task_id]
                    if existing["title"] != task_title:
                        await self.github.update_issue(
                            existing["number"],
                            title=task_title
                        )
                        result.updated += 1
                        logger.info(f"Updated issue #{existing['number']}: {task_title}")
                    else:
                        result.unchanged += 1
                        # Log progress every 20 skipped items
                        if result.unchanged % 20 == 0:
                            logger.info(f"Progress: skipped {result.unchanged} existing, {result.created} created ({idx+1}/{total_tasks})")
                else:
                    # Create new issue with correct priority label
                    full_body = self._format_issue_body(task_body, task_id, source_file)
                    await self.github.create_issue(
                        title=task_title,
                        body=full_body,
                        labels=["orchestra-task", "status:available", f"priority:{priority}"]
                    )
                    result.created += 1
                    logger.info(f"[{idx+1}/{total_tasks}] Created [{priority}]: {task_title[:50]}...")

                    # 2 second delay to avoid GitHub secondary rate limits
                    # Secondary limits are stricter than primary (content creation spam prevention)
                    await asyncio.sleep(2.0)

            except Exception as e:
                error_msg = f"Error syncing task '{task_title}': {e}"
                result.errors.append(error_msg)
                logger.error(error_msg)

        logger.info(f"Sync complete: {result.created} created, {result.updated} updated, "
                   f"{result.unchanged} unchanged, {len(result.errors)} errors")
        return result

    def _parse_todo_file(self, file_path: Path) -> List[Tuple[str, str, str, str, str]]:
        """
        Parse a TODO.md file into tasks.

        Returns list of (title, body, task_id, source_file, priority) tuples.
        Priority is detected from section headers like "## High Priority".
        """
        tasks = []
        content = file_path.read_text()

        # Track current priority based on section headers
        current_priority = "medium"  # Default priority

        lines = content.split('\n')
        i = 0
        while i < len(lines):
            line = lines[i]

            # Detect priority section headers
            line_lower = line.lower()
            if line.startswith('#'):
                if 'highest' in line_lower and 'priority' in line_lower:
                    current_priority = "highest"
                elif 'high' in line_lower and 'priority' in line_lower:
                    current_priority = "high"
                elif 'medium' in line_lower and 'priority' in line_lower:
                    current_priority = "medium"
                elif 'low' in line_lower and 'priority' in line_lower:
                    current_priority = "low"
                elif 'completed' in line_lower or 'done' in line_lower:
                    current_priority = "skip"  # Skip completed section
                elif 'detailed' in line_lower or 'documentation' in line_lower:
                    current_priority = "skip"  # Skip detailed docs section
                i += 1
                continue

            # Skip if in completed section
            if current_priority == "skip":
                i += 1
                continue

            # Match task items: - [ ] Task description
            match = re.match(r'^- \[ \] (.+)$', line)
            if match:
                title = match.group(1).strip()

                # Collect body (sub-items, description) until next task or section
                body_lines = []
                j = i + 1
                while j < len(lines):
                    next_line = lines[j]
                    # Stop at next task, section header, or empty line followed by non-indented content
                    if re.match(r'^- \[[ x]\]', next_line) or next_line.startswith('#'):
                        break
                    if next_line.strip():
                        body_lines.append(next_line)
                    j += 1

                body = '\n'.join(body_lines).strip()

                # Generate task ID from title
                task_id = f"task-{hashlib.sha256(title.encode()).hexdigest()[:8]}"

                tasks.append((title, body, task_id, str(file_path.relative_to(self.project_path)), current_priority))

            i += 1

        return tasks

    def _format_issue_body(self, description: str, task_id: str, source_file: str) -> str:
        """Format the issue body with metadata."""
        body = "## Description\n\n"
        body += description if description else "_No additional description._"
        body += "\n\n---\n\n"
        body += "## Metadata\n\n"
        body += f"- **Source**: `{source_file}`\n"
        body += f"- **Task ID**: `{task_id}`\n"
        body += "\n_This issue was created by Claude Orchestra task sync._"
        return body

    def _extract_task_id_from_body(self, body: str) -> Optional[str]:
        """Extract task ID from issue body."""
        match = re.search(r'\*\*Task ID\*\*: `(task-[a-f0-9]+)`', body)
        return match.group(1) if match else None

    # -------------------------------------------------------------------------
    # Task Discovery
    # -------------------------------------------------------------------------

    async def get_available_tasks(
        self,
        priority: Optional[str] = None,
        size: Optional[str] = None,
        limit: int = 10
    ) -> List[Task]:
        """
        Get tasks available for claiming.

        Returns tasks sorted by priority (high > medium > low).
        """
        labels = ["orchestra-task", "status:available"]
        if priority:
            labels.append(f"priority:{priority}")
        if size:
            labels.append(f"size:{size}")

        issues = await self.github.list_issues(
            labels=labels,
            assignee="none",
            per_page=limit
        )

        tasks = [self._issue_to_task(issue) for issue in issues]

        # Sort by priority
        priority_order = {"high": 0, "medium": 1, "low": 2, None: 3}
        tasks.sort(key=lambda t: priority_order.get(t.priority.value if t.priority else None, 3))

        return tasks

    async def get_my_claimed_tasks(self) -> List[Task]:
        """Get tasks currently claimed by this agent."""
        if not self.agent:
            return []

        issues = await self.github.list_issues(
            labels=["orchestra-task"],
            assignee=self.agent.github_username
        )

        return [self._issue_to_task(issue) for issue in issues]

    async def get_all_active_claims(self) -> List[ClaimInfo]:
        """Get all active claims across all agents."""
        issues = await self.github.list_issues(
            labels=["orchestra-task", "status:claimed"]
        )
        issues.extend(await self.github.list_issues(
            labels=["orchestra-task", "status:in-progress"]
        ))

        claims = []
        for issue in issues:
            claim = await self._get_claim_info(issue["number"])
            if claim:
                claims.append(claim)

        return claims

    def _issue_to_task(self, issue: Dict) -> Task:
        """Convert GitHub issue dict to Task object."""
        labels = [l["name"] for l in issue.get("labels", [])]

        # Extract status
        status = TaskStatus.AVAILABLE
        for label in labels:
            if label.startswith("status:"):
                try:
                    status = TaskStatus(label.split(":")[1])
                except ValueError:
                    pass

        # Extract priority
        priority = None
        for label in labels:
            if label.startswith("priority:"):
                try:
                    priority = TaskPriority(label.split(":")[1])
                except ValueError:
                    pass

        # Extract size
        size = None
        for label in labels:
            if label.startswith("size:"):
                try:
                    size = TaskSize(label.split(":")[1])
                except ValueError:
                    pass

        return Task(
            issue_number=issue["number"],
            title=issue["title"],
            description=issue.get("body", ""),
            status=status,
            priority=priority,
            size=size,
            assignee=issue.get("assignee", {}).get("login") if issue.get("assignee") else None,
            labels=labels,
            created_at=issue.get("created_at"),
            updated_at=issue.get("updated_at"),
            task_id=self._extract_task_id_from_body(issue.get("body", ""))
        )

    # -------------------------------------------------------------------------
    # Claiming
    # -------------------------------------------------------------------------

    async def claim_task(
        self,
        issue_number: int,
        branch_name: Optional[str] = None
    ) -> ClaimResult:
        """
        Attempt to claim a task.

        Uses GitHub's assignee field for atomic claiming.
        """
        if not self.agent:
            await self.setup()

        try:
            # Check current state
            issue = await self.github.get_issue(issue_number)

            if issue.get("assignee") is not None:
                return ClaimResult(
                    success=False,
                    issue_number=issue_number,
                    reason="already_assigned"
                )

            labels = [l["name"] for l in issue.get("labels", [])]
            if "status:available" not in labels:
                return ClaimResult(
                    success=False,
                    issue_number=issue_number,
                    reason="not_available"
                )

            # Generate branch name if not provided
            if not branch_name:
                branch_name = f"{self.agent.user}/task/{issue_number}"

            # Attempt to assign
            await self.github.update_issue(
                issue_number,
                assignee=self.agent.github_username
            )

            # Verify we got it (race condition check)
            issue = await self.github.get_issue(issue_number)
            if issue.get("assignee", {}).get("login") != self.agent.github_username:
                return ClaimResult(
                    success=False,
                    issue_number=issue_number,
                    reason="race_condition"
                )

            # Update labels
            await self.github.remove_label(issue_number, "status:available")
            await self.github.add_labels(issue_number, ["status:claimed"])

            # Add claim comment
            claim_info = ClaimInfo(
                issue_number=issue_number,
                agent_id=self.agent.agent_id,
                github_username=self.agent.github_username,
                claimed_at=datetime.now(timezone.utc).isoformat(),
                last_heartbeat=datetime.now(timezone.utc).isoformat(),
                branch_name=branch_name
            )

            comment = await self.github.create_comment(
                issue_number,
                self._format_claim_comment(claim_info)
            )

            # Track the claim
            self._claimed_issues[issue_number] = claim_info
            self._claim_comment_ids[issue_number] = comment["id"]

            logger.info(f"Successfully claimed issue #{issue_number}")

            return ClaimResult(
                success=True,
                issue_number=issue_number,
                task=self._issue_to_task(issue),
                branch_name=branch_name,
                claim_info=claim_info
            )

        except GitHubAPIError as e:
            logger.error(f"GitHub API error claiming #{issue_number}: {e}")
            return ClaimResult(
                success=False,
                issue_number=issue_number,
                reason=f"api_error: {e}"
            )

    async def claim_next_available(
        self,
        priority: Optional[str] = None,
        size: Optional[str] = None
    ) -> ClaimResult:
        """
        Claim the next available task matching criteria.

        Iterates through available tasks until one is successfully claimed.
        """
        tasks = await self.get_available_tasks(priority=priority, size=size)

        if not tasks:
            return ClaimResult(success=False, reason="no_tasks_available")

        for task in tasks:
            result = await self.claim_task(task.issue_number)
            if result.success:
                return result

            # Small delay before trying next
            await asyncio.sleep(0.5)

        return ClaimResult(success=False, reason="all_tasks_claimed")

    async def release_claim(
        self,
        issue_number: int,
        reason: str = "released"
    ) -> None:
        """Release a claim without completing the task."""
        if not self.agent:
            return

        try:
            # Remove assignee
            await self.github.update_issue(
                issue_number,
                assignee=""  # Empty string unassigns
            )

            # Update labels
            await self.github.remove_label(issue_number, "status:claimed")
            await self.github.remove_label(issue_number, "status:in-progress")
            await self.github.add_labels(issue_number, ["status:available"])

            # Add comment
            await self.github.create_comment(
                issue_number,
                f"## Claim Released\n\n"
                f"Agent `{self.agent.agent_id}` released this task.\n\n"
                f"**Reason**: {reason}\n\n"
                f"_Task is now available for claiming._"
            )

            # Remove from tracking
            self._claimed_issues.pop(issue_number, None)
            self._claim_comment_ids.pop(issue_number, None)

            logger.info(f"Released claim on issue #{issue_number}")

        except GitHubAPIError as e:
            logger.error(f"Error releasing claim on #{issue_number}: {e}")

    def _format_claim_comment(self, claim: ClaimInfo) -> str:
        """Format the claim comment with metadata."""
        return f"""{self.CLAIM_COMMENT_MARKER}
## Task Claimed

| Field | Value |
|-------|-------|
| Agent ID | `{claim.agent_id}` |
| GitHub User | @{claim.github_username} |
| Claimed At | {claim.claimed_at} |
| Branch | `{claim.branch_name or 'TBD'}` |
| Heartbeat | {claim.last_heartbeat} |
{f'| Progress | {claim.progress_note} |' if claim.progress_note else ''}

---
_This claim will expire if no heartbeat for {self.claim_timeout // 60} minutes._
"""

    async def _get_claim_info(self, issue_number: int) -> Optional[ClaimInfo]:
        """Extract claim info from issue comments."""
        comments = await self.github.get_comments(issue_number)

        for comment in reversed(comments):  # Most recent first
            body = comment.get("body", "")
            if self.CLAIM_COMMENT_MARKER in body:
                return self._parse_claim_comment(body, issue_number)

        return None

    def _parse_claim_comment(self, body: str, issue_number: int) -> Optional[ClaimInfo]:
        """Parse claim info from comment body."""
        try:
            agent_match = re.search(r'Agent ID \| `([^`]+)`', body)
            user_match = re.search(r'GitHub User \| @(\S+)', body)
            claimed_match = re.search(r'Claimed At \| ([^\n|]+)', body)
            heartbeat_match = re.search(r'Heartbeat \| ([^\n|]+)', body)
            branch_match = re.search(r'Branch \| `([^`]+)`', body)

            if agent_match and user_match and claimed_match and heartbeat_match:
                return ClaimInfo(
                    issue_number=issue_number,
                    agent_id=agent_match.group(1).strip(),
                    github_username=user_match.group(1).strip(),
                    claimed_at=claimed_match.group(1).strip(),
                    last_heartbeat=heartbeat_match.group(1).strip(),
                    branch_name=branch_match.group(1).strip() if branch_match else None
                )
        except Exception as e:
            logger.warning(f"Failed to parse claim comment: {e}")

        return None

    # -------------------------------------------------------------------------
    # Progress Tracking
    # -------------------------------------------------------------------------

    async def update_progress(
        self,
        issue_number: int,
        status: Optional[str] = None,
        progress_note: Optional[str] = None
    ) -> None:
        """Update task progress and heartbeat."""
        if issue_number not in self._claimed_issues:
            logger.warning(f"Cannot update progress for unclaimed issue #{issue_number}")
            return

        claim = self._claimed_issues[issue_number]
        claim.last_heartbeat = datetime.now(timezone.utc).isoformat()
        if progress_note:
            claim.progress_note = progress_note

        # Update status label if provided
        if status and status != "claimed":
            await self.github.remove_label(issue_number, "status:claimed")
            await self.github.add_labels(issue_number, [f"status:{status}"])

        # Update claim comment
        if issue_number in self._claim_comment_ids:
            await self.github.update_comment(
                self._claim_comment_ids[issue_number],
                self._format_claim_comment(claim)
            )

    async def start_heartbeat_loop(self) -> asyncio.Task:
        """Start background task that updates heartbeats for all claimed tasks."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            return self._heartbeat_task

        async def heartbeat_loop():
            while True:
                try:
                    await asyncio.sleep(self.heartbeat_interval)
                    for issue_number in list(self._claimed_issues.keys()):
                        try:
                            await self.update_progress(issue_number)
                            logger.debug(f"Heartbeat updated for #{issue_number}")
                        except Exception as e:
                            logger.warning(f"Failed heartbeat for #{issue_number}: {e}")
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    logger.error(f"Error in heartbeat loop: {e}")

        self._heartbeat_task = asyncio.create_task(heartbeat_loop())
        logger.info("Heartbeat loop started")
        return self._heartbeat_task

    async def stop_heartbeat_loop(self) -> None:
        """Stop the heartbeat loop."""
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass
            logger.info("Heartbeat loop stopped")

    # -------------------------------------------------------------------------
    # Completion
    # -------------------------------------------------------------------------

    async def mark_pr_created(
        self,
        issue_number: int,
        pr_number: int
    ) -> None:
        """Mark that a PR was created for this task."""
        # Update label
        await self.github.remove_label(issue_number, "status:in-progress")
        await self.github.remove_label(issue_number, "status:claimed")
        await self.github.add_labels(issue_number, ["status:review"])

        # Add comment
        await self.github.create_comment(
            issue_number,
            f"## PR Created\n\n"
            f"Pull request #{pr_number} has been created for this task.\n\n"
            f"Waiting for review and merge."
        )

        logger.info(f"Marked PR #{pr_number} created for issue #{issue_number}")

    async def complete_task(
        self,
        issue_number: int,
        pr_number: Optional[int] = None,
        summary: Optional[str] = None
    ) -> None:
        """Mark task as complete."""
        comment_body = "## Task Completed\n\n"

        if pr_number:
            comment_body += f"Completed via PR #{pr_number}.\n\n"

        if summary:
            comment_body += f"**Summary**: {summary}\n\n"

        if self.agent:
            comment_body += f"_Completed by agent `{self.agent.agent_id}`_"

        await self.github.create_comment(issue_number, comment_body)

        # Close the issue
        await self.github.update_issue(issue_number, state="closed")

        # Cleanup tracking
        self._claimed_issues.pop(issue_number, None)
        self._claim_comment_ids.pop(issue_number, None)

        logger.info(f"Completed task #{issue_number}")

    async def mark_blocked(
        self,
        issue_number: int,
        reason: str
    ) -> None:
        """Mark task as blocked."""
        # Update labels
        await self.github.remove_label(issue_number, "status:claimed")
        await self.github.remove_label(issue_number, "status:in-progress")
        await self.github.add_labels(issue_number, ["status:blocked"])

        # Remove assignment so humans can investigate
        await self.github.update_issue(issue_number, assignee="")

        # Add comment
        await self.github.create_comment(
            issue_number,
            f"## Task Blocked\n\n"
            f"**Reason**: {reason}\n\n"
            f"Agent `{self.agent.agent_id if self.agent else 'unknown'}` encountered a blocker.\n\n"
            f"_This task needs human intervention before it can proceed._"
        )

        # Cleanup tracking
        self._claimed_issues.pop(issue_number, None)
        self._claim_comment_ids.pop(issue_number, None)

        logger.info(f"Marked issue #{issue_number} as blocked: {reason}")

    # -------------------------------------------------------------------------
    # Stale Claim Management
    # -------------------------------------------------------------------------

    async def check_stale_claims(self) -> List[ClaimInfo]:
        """Find claims that have gone stale (no heartbeat within timeout)."""
        stale_claims = []
        now = datetime.now(timezone.utc)

        # Get all claimed/in-progress issues
        claimed_issues = await self.github.list_issues(
            labels=["orchestra-task", "status:claimed"],
            per_page=100
        )
        in_progress_issues = await self.github.list_issues(
            labels=["orchestra-task", "status:in-progress"],
            per_page=100
        )

        all_issues = claimed_issues + in_progress_issues

        for issue in all_issues:
            claim_info = await self._get_claim_info(issue["number"])
            if not claim_info:
                continue

            try:
                heartbeat = datetime.fromisoformat(
                    claim_info.last_heartbeat.replace('Z', '+00:00')
                )
                age_seconds = (now - heartbeat).total_seconds()

                if age_seconds > self.claim_timeout:
                    stale_claims.append(claim_info)
                    logger.info(
                        f"Found stale claim on #{issue['number']}: "
                        f"last heartbeat {age_seconds/60:.1f} min ago"
                    )
            except Exception as e:
                logger.warning(f"Error checking claim staleness: {e}")

        return stale_claims

    async def reclaim_stale_tasks(self) -> int:
        """
        Release claims that have gone stale.

        Returns number of stale claims released.
        """
        stale_claims = await self.check_stale_claims()
        released = 0

        for claim in stale_claims:
            try:
                issue_number = claim.issue_number

                # Remove assignee
                await self.github.update_issue(issue_number, assignee="")

                # Update labels
                await self.github.remove_label(issue_number, "status:claimed")
                await self.github.remove_label(issue_number, "status:in-progress")
                await self.github.add_labels(issue_number, ["status:available"])

                # Add comment
                await self.github.create_comment(
                    issue_number,
                    f"## Stale Claim Released\n\n"
                    f"Agent `{claim.agent_id}` stopped responding.\n\n"
                    f"**Last heartbeat**: {claim.last_heartbeat}\n"
                    f"**Timeout**: {self.claim_timeout // 60} minutes\n\n"
                    f"_Task is now available for claiming._"
                )

                released += 1
                logger.info(f"Released stale claim on #{issue_number}")

            except Exception as e:
                logger.error(f"Error releasing stale claim: {e}")

        return released

    # -------------------------------------------------------------------------
    # Utilities
    # -------------------------------------------------------------------------

    def get_branch_name(self, issue_number: int, title: Optional[str] = None) -> str:
        """Generate branch name for a task."""
        if not self.agent:
            user = os.getenv("USER") or "unknown"
        else:
            user = self.agent.user

        return f"{user}/task/{issue_number}"


# =============================================================================
# CLI Interface
# =============================================================================

async def main():
    """CLI entry point for task coordination commands."""
    import argparse

    parser = argparse.ArgumentParser(description="Task Coordinator CLI")
    parser.add_argument("command", choices=[
        "setup", "sync", "list", "claim", "release", "complete",
        "stale", "reclaim"
    ])
    parser.add_argument("--repo", help="Repository in owner/name format")
    parser.add_argument("--issue", type=int, help="Issue number")
    parser.add_argument("--priority", choices=["high", "medium", "low"])
    parser.add_argument("--size", choices=["small", "medium", "large"])
    parser.add_argument("--timeout", type=int, default=1800, help="Claim timeout in seconds")

    args = parser.parse_args()

    # Get configuration
    token = os.getenv("GITHUB_TOKEN")
    if not token:
        print("Error: GITHUB_TOKEN environment variable required")
        return 1

    if args.repo:
        owner, repo = args.repo.split("/")
    else:
        owner = os.getenv("GITHUB_REPO_OWNER", "")
        repo = os.getenv("GITHUB_REPO_NAME", "")

    if not owner or not repo:
        print("Error: Repository required (--repo owner/name or env vars)")
        return 1

    # Create coordinator
    coordinator = TaskCoordinator(
        repo_owner=owner,
        repo_name=repo,
        github_token=token,
        claim_timeout=args.timeout
    )

    try:
        await coordinator.setup()

        if args.command == "setup":
            print("Setup complete!")
            print(f"  Agent ID: {coordinator.agent.agent_id}")
            print(f"  Labels created/verified")

        elif args.command == "sync":
            result = await coordinator.sync_todos_to_issues()
            print(f"Sync complete:")
            print(f"  Created: {result.created}")
            print(f"  Updated: {result.updated}")
            print(f"  Unchanged: {result.unchanged}")
            if result.errors:
                print(f"  Errors: {len(result.errors)}")
                for err in result.errors:
                    print(f"    - {err}")

        elif args.command == "list":
            tasks = await coordinator.get_available_tasks(
                priority=args.priority,
                size=args.size
            )
            if not tasks:
                print("No available tasks")
            else:
                print(f"Available tasks ({len(tasks)}):")
                for task in tasks:
                    priority = f"[{task.priority.value}]" if task.priority else ""
                    size = f"[{task.size.value}]" if task.size else ""
                    print(f"  #{task.issue_number} {task.title} {priority} {size}")

        elif args.command == "claim":
            if args.issue:
                result = await coordinator.claim_task(args.issue)
            else:
                result = await coordinator.claim_next_available(
                    priority=args.priority,
                    size=args.size
                )

            if result.success:
                print(f"Claimed issue #{result.issue_number}")
                print(f"  Branch: {result.branch_name}")
            else:
                print(f"Failed to claim: {result.reason}")

        elif args.command == "release":
            if not args.issue:
                print("Error: --issue required")
                return 1
            await coordinator.release_claim(args.issue)
            print(f"Released claim on #{args.issue}")

        elif args.command == "complete":
            if not args.issue:
                print("Error: --issue required")
                return 1
            await coordinator.complete_task(args.issue)
            print(f"Completed #{args.issue}")

        elif args.command == "stale":
            stale = await coordinator.check_stale_claims()
            if not stale:
                print("No stale claims")
            else:
                print(f"Stale claims ({len(stale)}):")
                for claim in stale:
                    print(f"  #{claim.issue_number} - Agent: {claim.agent_id}")
                    print(f"    Last heartbeat: {claim.last_heartbeat}")

        elif args.command == "reclaim":
            count = await coordinator.reclaim_stale_tasks()
            print(f"Released {count} stale claims")

    finally:
        await coordinator.close()

    return 0


if __name__ == "__main__":
    import sys
    sys.exit(asyncio.run(main()))
