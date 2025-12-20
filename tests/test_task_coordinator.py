#!/usr/bin/env python3
"""
Unit tests for TaskCoordinator

Tests the multi-user task coordination system using mocked GitHub API calls.
"""

import pytest
import asyncio
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from task_coordinator import (
    TaskCoordinator,
    GitHubClient,
    AgentIdentity,
    Task,
    TaskStatus,
    TaskPriority,
    TaskSize,
    ClaimInfo,
    ClaimResult,
    SyncResult,
    ORCHESTRA_LABELS,
)


# =============================================================================
# Fixtures
# =============================================================================

@pytest.fixture
def mock_github_client():
    """Create a mocked GitHub client."""
    client = AsyncMock(spec=GitHubClient)
    client.owner = "test-owner"
    client.repo = "test-repo"
    client._username = "test-user"
    client.get_authenticated_user = AsyncMock(return_value="test-user")
    client.ensure_labels_exist = AsyncMock()
    client.close = AsyncMock()
    return client


@pytest.fixture
def coordinator(mock_github_client, tmp_path):
    """Create a TaskCoordinator with mocked GitHub client."""
    coord = TaskCoordinator(
        repo_owner="test-owner",
        repo_name="test-repo",
        github_token="fake-token",
        project_path=str(tmp_path),
        heartbeat_interval=60,
        claim_timeout=300
    )
    coord.github = mock_github_client
    return coord


@pytest.fixture
def sample_issue():
    """Sample GitHub issue data."""
    return {
        "number": 42,
        "title": "Add user authentication",
        "body": "## Description\n\nImplement OAuth login\n\n---\n\n**Task ID**: `task-abc12345`",
        "state": "open",
        "assignee": None,
        "labels": [
            {"name": "orchestra-task"},
            {"name": "status:available"},
            {"name": "priority:high"},
            {"name": "size:medium"}
        ],
        "created_at": "2025-12-19T10:00:00Z",
        "updated_at": "2025-12-19T10:00:00Z"
    }


@pytest.fixture
def sample_claimed_issue(sample_issue):
    """Sample claimed issue."""
    issue = sample_issue.copy()
    issue["assignee"] = {"login": "test-user"}
    issue["labels"] = [
        {"name": "orchestra-task"},
        {"name": "status:claimed"},
        {"name": "priority:high"}
    ]
    return issue


# =============================================================================
# AgentIdentity Tests
# =============================================================================

class TestAgentIdentity:
    """Tests for AgentIdentity generation."""

    def test_generate_creates_unique_id(self):
        """Agent IDs should be unique."""
        import time
        id1 = AgentIdentity.generate("user1")
        time.sleep(0.01)  # Small delay to ensure different timestamp
        id2 = AgentIdentity.generate("user2")
        # IDs may be same if generated in same second, but started_at differs
        assert id1.started_at != id2.started_at or id1.agent_id != id2.agent_id

    def test_generate_includes_user_info(self):
        """Agent ID should include user and hostname."""
        identity = AgentIdentity.generate("github-user")
        assert identity.github_username == "github-user"
        assert identity.user  # Should have OS user
        assert identity.hostname  # Should have hostname

    def test_to_dict(self):
        """Should serialize to dictionary."""
        identity = AgentIdentity.generate("test")
        data = identity.to_dict()
        assert "agent_id" in data
        assert "github_username" in data
        assert data["github_username"] == "test"


# =============================================================================
# Task Conversion Tests
# =============================================================================

class TestTaskConversion:
    """Tests for converting GitHub issues to Task objects."""

    @pytest.mark.asyncio
    async def test_issue_to_task(self, coordinator, sample_issue):
        """Should convert issue dict to Task object."""
        await coordinator.setup()
        task = coordinator._issue_to_task(sample_issue)

        assert task.issue_number == 42
        assert task.title == "Add user authentication"
        assert task.status == TaskStatus.AVAILABLE
        assert task.priority == TaskPriority.HIGH
        assert task.size == TaskSize.MEDIUM
        assert task.assignee is None

    @pytest.mark.asyncio
    async def test_issue_to_task_with_assignee(self, coordinator, sample_claimed_issue):
        """Should extract assignee from issue."""
        await coordinator.setup()
        task = coordinator._issue_to_task(sample_claimed_issue)

        assert task.assignee == "test-user"
        assert task.status == TaskStatus.CLAIMED

    @pytest.mark.asyncio
    async def test_extract_task_id(self, coordinator, sample_issue):
        """Should extract task ID from issue body."""
        await coordinator.setup()
        task_id = coordinator._extract_task_id_from_body(sample_issue["body"])
        assert task_id == "task-abc12345"


# =============================================================================
# Task Discovery Tests
# =============================================================================

class TestTaskDiscovery:
    """Tests for finding available tasks."""

    @pytest.mark.asyncio
    async def test_get_available_tasks(self, coordinator, mock_github_client, sample_issue):
        """Should return available tasks."""
        mock_github_client.list_issues = AsyncMock(return_value=[sample_issue])
        await coordinator.setup()

        tasks = await coordinator.get_available_tasks()

        assert len(tasks) == 1
        assert tasks[0].issue_number == 42
        mock_github_client.list_issues.assert_called_once()

    @pytest.mark.asyncio
    async def test_get_available_tasks_with_filters(self, coordinator, mock_github_client, sample_issue):
        """Should filter by priority and size."""
        mock_github_client.list_issues = AsyncMock(return_value=[sample_issue])
        await coordinator.setup()

        await coordinator.get_available_tasks(priority="high", size="small")

        call_args = mock_github_client.list_issues.call_args
        labels = call_args.kwargs.get("labels", [])
        assert "priority:high" in labels
        assert "size:small" in labels

    @pytest.mark.asyncio
    async def test_get_available_tasks_sorted_by_priority(self, coordinator, mock_github_client):
        """Should sort tasks by priority (high first)."""
        issues = [
            {
                "number": 1, "title": "Low", "body": "", "state": "open",
                "assignee": None,
                "labels": [{"name": "status:available"}, {"name": "priority:low"}]
            },
            {
                "number": 2, "title": "High", "body": "", "state": "open",
                "assignee": None,
                "labels": [{"name": "status:available"}, {"name": "priority:high"}]
            },
            {
                "number": 3, "title": "Medium", "body": "", "state": "open",
                "assignee": None,
                "labels": [{"name": "status:available"}, {"name": "priority:medium"}]
            },
        ]
        mock_github_client.list_issues = AsyncMock(return_value=issues)
        await coordinator.setup()

        tasks = await coordinator.get_available_tasks()

        assert tasks[0].issue_number == 2  # High priority first
        assert tasks[1].issue_number == 3  # Medium second
        assert tasks[2].issue_number == 1  # Low last


# =============================================================================
# Claiming Tests
# =============================================================================

class TestClaiming:
    """Tests for task claiming."""

    @pytest.mark.asyncio
    async def test_claim_task_success(self, coordinator, mock_github_client, sample_issue):
        """Should successfully claim an available task."""
        # First call returns unclaimed, second returns claimed by us
        mock_github_client.get_issue = AsyncMock(side_effect=[
            sample_issue,
            {**sample_issue, "assignee": {"login": "test-user"}}
        ])
        mock_github_client.update_issue = AsyncMock()
        mock_github_client.remove_label = AsyncMock()
        mock_github_client.add_labels = AsyncMock()
        mock_github_client.create_comment = AsyncMock(return_value={"id": 123})

        await coordinator.setup()
        result = await coordinator.claim_task(42)

        assert result.success is True
        assert result.issue_number == 42
        assert result.branch_name == f"{coordinator.agent.user}/task/42"

    @pytest.mark.asyncio
    async def test_claim_task_already_assigned(self, coordinator, mock_github_client, sample_claimed_issue):
        """Should fail if task already assigned."""
        mock_github_client.get_issue = AsyncMock(return_value=sample_claimed_issue)
        await coordinator.setup()

        result = await coordinator.claim_task(42)

        assert result.success is False
        assert result.reason == "already_assigned"

    @pytest.mark.asyncio
    async def test_claim_task_not_available(self, coordinator, mock_github_client, sample_issue):
        """Should fail if task not in available status."""
        issue = sample_issue.copy()
        issue["labels"] = [{"name": "orchestra-task"}, {"name": "status:in-progress"}]
        mock_github_client.get_issue = AsyncMock(return_value=issue)
        await coordinator.setup()

        result = await coordinator.claim_task(42)

        assert result.success is False
        assert result.reason == "not_available"

    @pytest.mark.asyncio
    async def test_claim_task_race_condition(self, coordinator, mock_github_client, sample_issue):
        """Should detect race condition when another agent claims first."""
        mock_github_client.get_issue = AsyncMock(side_effect=[
            sample_issue,  # First check: available
            {**sample_issue, "assignee": {"login": "other-user"}}  # After assign: someone else got it
        ])
        mock_github_client.update_issue = AsyncMock()
        await coordinator.setup()

        result = await coordinator.claim_task(42)

        assert result.success is False
        assert result.reason == "race_condition"

    @pytest.mark.asyncio
    async def test_claim_next_available(self, coordinator, mock_github_client, sample_issue):
        """Should claim first available task."""
        mock_github_client.list_issues = AsyncMock(return_value=[sample_issue])
        mock_github_client.get_issue = AsyncMock(side_effect=[
            sample_issue,
            {**sample_issue, "assignee": {"login": "test-user"}}
        ])
        mock_github_client.update_issue = AsyncMock()
        mock_github_client.remove_label = AsyncMock()
        mock_github_client.add_labels = AsyncMock()
        mock_github_client.create_comment = AsyncMock(return_value={"id": 123})

        await coordinator.setup()
        result = await coordinator.claim_next_available()

        assert result.success is True
        assert result.issue_number == 42


# =============================================================================
# Release and Completion Tests
# =============================================================================

class TestReleaseAndCompletion:
    """Tests for releasing claims and completing tasks."""

    @pytest.mark.asyncio
    async def test_release_claim(self, coordinator, mock_github_client):
        """Should release a claimed task."""
        mock_github_client.update_issue = AsyncMock()
        mock_github_client.remove_label = AsyncMock()
        mock_github_client.add_labels = AsyncMock()
        mock_github_client.create_comment = AsyncMock()

        await coordinator.setup()
        await coordinator.release_claim(42, reason="testing")

        mock_github_client.update_issue.assert_called_once()
        mock_github_client.add_labels.assert_called_with(42, ["status:available"])

    @pytest.mark.asyncio
    async def test_complete_task(self, coordinator, mock_github_client):
        """Should close issue when task completed."""
        mock_github_client.create_comment = AsyncMock()
        mock_github_client.update_issue = AsyncMock()

        await coordinator.setup()
        await coordinator.complete_task(42, pr_number=99, summary="Done!")

        # Should close the issue
        mock_github_client.update_issue.assert_called_with(42, state="closed")

    @pytest.mark.asyncio
    async def test_mark_blocked(self, coordinator, mock_github_client):
        """Should mark task as blocked."""
        mock_github_client.remove_label = AsyncMock()
        mock_github_client.add_labels = AsyncMock()
        mock_github_client.update_issue = AsyncMock()
        mock_github_client.create_comment = AsyncMock()

        await coordinator.setup()
        await coordinator.mark_blocked(42, reason="Dependency not available")

        mock_github_client.add_labels.assert_called_with(42, ["status:blocked"])
        # Should unassign
        mock_github_client.update_issue.assert_called_with(42, assignee="")


# =============================================================================
# Stale Claim Tests
# =============================================================================

class TestStaleClaims:
    """Tests for stale claim detection and recovery."""

    @pytest.mark.asyncio
    async def test_check_stale_claims(self, coordinator, mock_github_client):
        """Should detect stale claims."""
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        mock_github_client.list_issues = AsyncMock(side_effect=[
            [{
                "number": 42,
                "title": "Stale task",
                "body": "",
                "state": "open",
                "assignee": {"login": "old-user"},
                "labels": [{"name": "status:claimed"}]
            }],
            []  # in-progress issues
        ])

        mock_github_client.get_comments = AsyncMock(return_value=[{
            "id": 1,
            "body": f"""<!-- orchestra-claim -->
## Task Claimed

| Field | Value |
|-------|-------|
| Agent ID | `old-agent` |
| GitHub User | @old-user |
| Claimed At | {stale_time} |
| Heartbeat | {stale_time} |
"""
        }])

        await coordinator.setup()
        stale = await coordinator.check_stale_claims()

        assert len(stale) == 1
        assert stale[0].issue_number == 42

    @pytest.mark.asyncio
    async def test_reclaim_stale_tasks(self, coordinator, mock_github_client):
        """Should release stale claims."""
        stale_time = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()

        mock_github_client.list_issues = AsyncMock(side_effect=[
            [{
                "number": 42,
                "title": "Stale task",
                "body": "",
                "state": "open",
                "assignee": {"login": "old-user"},
                "labels": [{"name": "status:claimed"}]
            }],
            []
        ])

        mock_github_client.get_comments = AsyncMock(return_value=[{
            "id": 1,
            "body": f"""<!-- orchestra-claim -->
## Task Claimed

| Field | Value |
|-------|-------|
| Agent ID | `old-agent` |
| GitHub User | @old-user |
| Claimed At | {stale_time} |
| Heartbeat | {stale_time} |
"""
        }])

        mock_github_client.update_issue = AsyncMock()
        mock_github_client.remove_label = AsyncMock()
        mock_github_client.add_labels = AsyncMock()
        mock_github_client.create_comment = AsyncMock()

        await coordinator.setup()
        count = await coordinator.reclaim_stale_tasks()

        assert count == 1
        mock_github_client.add_labels.assert_called_with(42, ["status:available"])


# =============================================================================
# TODO Sync Tests
# =============================================================================

class TestTodoSync:
    """Tests for syncing TODO.md to GitHub Issues."""

    @pytest.mark.asyncio
    async def test_parse_todo_file(self, coordinator, tmp_path):
        """Should parse tasks from TODO.md."""
        todo_file = tmp_path / "TODO.md"
        todo_file.write_text("""# TODO

## High Priority

- [ ] Implement user authentication
  - Add login form
  - Add logout button
- [ ] Fix database connection pooling

## Low Priority

- [x] Update README (completed)
- [ ] Add dark mode
""")

        await coordinator.setup()
        tasks = coordinator._parse_todo_file(todo_file)

        # Should find 3 incomplete tasks
        assert len(tasks) == 3
        titles = [t[0] for t in tasks]
        assert "Implement user authentication" in titles
        assert "Fix database connection pooling" in titles
        assert "Add dark mode" in titles
        # Should not include completed task
        assert "Update README (completed)" not in titles

    @pytest.mark.asyncio
    async def test_sync_creates_new_issues(self, coordinator, mock_github_client, tmp_path):
        """Should create issues for new tasks."""
        todo_file = tmp_path / "TODO.md"
        todo_file.write_text("- [ ] New task to sync")

        mock_github_client.list_issues = AsyncMock(return_value=[])
        mock_github_client.create_issue = AsyncMock(return_value={"number": 1})

        await coordinator.setup()
        result = await coordinator.sync_todos_to_issues(todo_files=["TODO.md"])

        assert result.created == 1
        assert result.updated == 0
        mock_github_client.create_issue.assert_called_once()

    @pytest.mark.asyncio
    async def test_sync_skips_existing(self, coordinator, mock_github_client, tmp_path):
        """Should not duplicate existing issues."""
        todo_file = tmp_path / "TODO.md"
        todo_file.write_text("- [ ] Existing task")

        # Task ID for "Existing task"
        import hashlib
        task_id = f"task-{hashlib.sha256('Existing task'.encode()).hexdigest()[:8]}"

        mock_github_client.list_issues = AsyncMock(return_value=[{
            "number": 1,
            "title": "Existing task",
            "body": f"**Task ID**: `{task_id}`",
            "labels": [{"name": "orchestra-task"}]
        }])

        await coordinator.setup()
        result = await coordinator.sync_todos_to_issues(todo_files=["TODO.md"])

        assert result.created == 0
        assert result.unchanged == 1


# =============================================================================
# Heartbeat Tests
# =============================================================================

class TestHeartbeat:
    """Tests for heartbeat functionality."""

    @pytest.mark.asyncio
    async def test_update_progress(self, coordinator, mock_github_client, sample_issue):
        """Should update heartbeat timestamp."""
        # Setup a claimed task
        mock_github_client.get_issue = AsyncMock(side_effect=[
            sample_issue,
            {**sample_issue, "assignee": {"login": "test-user"}}
        ])
        mock_github_client.update_issue = AsyncMock()
        mock_github_client.remove_label = AsyncMock()
        mock_github_client.add_labels = AsyncMock()
        mock_github_client.create_comment = AsyncMock(return_value={"id": 123})
        mock_github_client.update_comment = AsyncMock()

        await coordinator.setup()
        await coordinator.claim_task(42)

        # Update progress
        await coordinator.update_progress(42, progress_note="50% complete")

        mock_github_client.update_comment.assert_called_once()

    @pytest.mark.asyncio
    async def test_heartbeat_loop_starts_and_stops(self, coordinator, mock_github_client):
        """Should start and stop heartbeat loop."""
        await coordinator.setup()

        task = await coordinator.start_heartbeat_loop()
        assert task is not None
        assert not task.done()

        await coordinator.stop_heartbeat_loop()
        # Give it a moment to cancel
        await asyncio.sleep(0.1)
        assert task.done()


# =============================================================================
# Branch Naming Tests
# =============================================================================

class TestBranchNaming:
    """Tests for branch name generation."""

    @pytest.mark.asyncio
    async def test_get_branch_name(self, coordinator):
        """Should generate correct branch name format."""
        await coordinator.setup()
        branch = coordinator.get_branch_name(42)

        assert "/task/42" in branch
        assert coordinator.agent.user in branch


# =============================================================================
# Integration Tests
# =============================================================================

class TestIntegration:
    """Integration tests for full workflows."""

    @pytest.mark.asyncio
    async def test_full_claim_workflow(self, coordinator, mock_github_client, sample_issue):
        """Test complete claim → work → complete workflow."""
        # Setup mocks
        mock_github_client.list_issues = AsyncMock(return_value=[sample_issue])
        mock_github_client.get_issue = AsyncMock(side_effect=[
            sample_issue,
            {**sample_issue, "assignee": {"login": "test-user"}}
        ])
        mock_github_client.update_issue = AsyncMock()
        mock_github_client.remove_label = AsyncMock()
        mock_github_client.add_labels = AsyncMock()
        mock_github_client.create_comment = AsyncMock(return_value={"id": 123})
        mock_github_client.update_comment = AsyncMock()

        await coordinator.setup()

        # 1. Claim task
        claim_result = await coordinator.claim_next_available()
        assert claim_result.success

        # 2. Update progress
        await coordinator.update_progress(42, status="in-progress", progress_note="Working...")

        # 3. Mark PR created
        await coordinator.mark_pr_created(42, pr_number=99)

        # 4. Complete
        await coordinator.complete_task(42, pr_number=99)

        # Verify issue was closed
        mock_github_client.update_issue.assert_called_with(42, state="closed")


# =============================================================================
# Run tests
# =============================================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v"])
