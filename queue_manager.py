#!/usr/bin/env python3
"""
Queue Manager - File-based message queue for orchestra communication.

Provides a persistent queue that the dashboard can write to and the orchestra
can read from, enabling cross-process communication for task prioritization.
"""

import json
import os
import fcntl
from pathlib import Path
from datetime import datetime
from typing import Optional, Dict, List, Any


class QueueManager:
    """
    File-based message queue for dashboard-to-orchestra communication.

    Uses file locking to ensure safe concurrent access between
    the dashboard process and orchestra subprocess.
    """

    QUEUE_FILENAME = ".orchestra_queue.json"

    def __init__(self, base_path: Optional[str] = None):
        """
        Initialize queue manager.

        Args:
            base_path: Base directory for queue file. Defaults to script directory.
        """
        if base_path:
            self.queue_path = Path(base_path) / self.QUEUE_FILENAME
        else:
            self.queue_path = Path(__file__).parent / self.QUEUE_FILENAME

    def _read_queue(self) -> Dict[str, Any]:
        """Read queue from file with locking."""
        if not self.queue_path.exists():
            return {"messages": [], "counter": 0}

        try:
            with open(self.queue_path, 'r') as f:
                fcntl.flock(f.fileno(), fcntl.LOCK_SH)
                try:
                    data = json.load(f)
                finally:
                    fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                return data
        except (json.JSONDecodeError, IOError):
            return {"messages": [], "counter": 0}

    def _write_queue(self, data: Dict[str, Any]) -> None:
        """Write queue to file with locking."""
        with open(self.queue_path, 'w') as f:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            try:
                json.dump(data, f, indent=2, default=str)
            finally:
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)

    def add_message(
        self,
        message: str,
        project_id: Optional[str] = None,
        priority: str = "normal"
    ) -> Dict[str, Any]:
        """
        Add a message to the queue.

        Args:
            message: The message/task content
            project_id: Optional project ID to target
            priority: "high", "normal", or "low"

        Returns:
            The created queue item
        """
        data = self._read_queue()
        data["counter"] += 1

        item = {
            "id": data["counter"],
            "message": message,
            "project_id": project_id,
            "priority": priority,
            "status": "pending",
            "created_at": datetime.now().isoformat()
        }

        # Insert based on priority
        if priority == "high":
            # Insert at front of pending items
            insert_idx = 0
            for i, msg in enumerate(data["messages"]):
                if msg["status"] != "pending":
                    break
                if msg["priority"] != "high":
                    insert_idx = i
                    break
                insert_idx = i + 1
            data["messages"].insert(insert_idx, item)
        else:
            data["messages"].append(item)

        self._write_queue(data)
        return item

    def get_next_pending(
        self,
        project_id: Optional[str] = None
    ) -> Optional[Dict[str, Any]]:
        """
        Get the next pending message for a project.

        Args:
            project_id: Optional project ID filter

        Returns:
            The next pending message, or None if queue is empty
        """
        data = self._read_queue()

        for item in data["messages"]:
            if item["status"] != "pending":
                continue
            if project_id and item["project_id"] and item["project_id"] != project_id:
                continue
            return item

        return None

    def claim_message(self, message_id: int) -> bool:
        """
        Mark a message as being processed.

        Args:
            message_id: ID of the message to claim

        Returns:
            True if claimed, False if not found or already claimed
        """
        data = self._read_queue()

        for item in data["messages"]:
            if item["id"] == message_id:
                if item["status"] != "pending":
                    return False
                item["status"] = "processing"
                item["claimed_at"] = datetime.now().isoformat()
                self._write_queue(data)
                return True

        return False

    def complete_message(
        self,
        message_id: int,
        success: bool = True,
        result: Optional[str] = None
    ) -> bool:
        """
        Mark a message as completed.

        Args:
            message_id: ID of the message
            success: Whether processing was successful
            result: Optional result description

        Returns:
            True if updated, False if not found
        """
        data = self._read_queue()

        for item in data["messages"]:
            if item["id"] == message_id:
                item["status"] = "completed" if success else "failed"
                item["completed_at"] = datetime.now().isoformat()
                if result:
                    item["result"] = result
                self._write_queue(data)
                return True

        return False

    def get_status(self) -> Dict[str, Any]:
        """
        Get queue status summary.

        Returns:
            Dict with queue statistics
        """
        data = self._read_queue()
        messages = data["messages"]

        return {
            "total": len(messages),
            "pending": len([m for m in messages if m["status"] == "pending"]),
            "processing": len([m for m in messages if m["status"] == "processing"]),
            "completed": len([m for m in messages if m["status"] == "completed"]),
            "failed": len([m for m in messages if m["status"] == "failed"]),
            "messages": messages[-50:]  # Last 50 for display
        }

    def get_all_pending(self, project_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get all pending messages.

        Args:
            project_id: Optional project ID filter

        Returns:
            List of pending messages
        """
        data = self._read_queue()
        pending = []

        for item in data["messages"]:
            if item["status"] != "pending":
                continue
            if project_id and item["project_id"] and item["project_id"] != project_id:
                continue
            pending.append(item)

        return pending

    def clear_completed(self, max_age_hours: int = 24) -> int:
        """
        Remove completed/failed messages older than max_age_hours.

        Returns:
            Number of messages removed
        """
        data = self._read_queue()
        cutoff = datetime.now().timestamp() - (max_age_hours * 3600)

        original_count = len(data["messages"])
        data["messages"] = [
            m for m in data["messages"]
            if m["status"] in ("pending", "processing") or
            datetime.fromisoformat(m.get("completed_at", m["created_at"])).timestamp() > cutoff
        ]

        removed = original_count - len(data["messages"])
        if removed > 0:
            self._write_queue(data)

        return removed


# Singleton instance for easy import
_default_queue: Optional[QueueManager] = None


def get_queue(base_path: Optional[str] = None) -> QueueManager:
    """Get or create the default queue manager."""
    global _default_queue
    if _default_queue is None or base_path:
        _default_queue = QueueManager(base_path)
    return _default_queue
