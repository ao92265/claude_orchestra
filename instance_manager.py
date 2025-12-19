#!/usr/bin/env python3
"""
Instance Manager - Multi-user/Multi-instance isolation for Claude Orchestra

Provides unique instance identifiers, port allocation, and coordination
to prevent conflicts when multiple users/instances run on the same machine.
"""

import os
import socket
import hashlib
import json
import fcntl
import time
from pathlib import Path
from typing import Optional, Dict, List
from dataclasses import dataclass, asdict
from datetime import datetime


@dataclass
class InstanceInfo:
    """Information about a running Claude Orchestra instance."""
    instance_id: str
    user: str
    hostname: str
    project_path: str
    pid: int
    dashboard_port: Optional[int]
    started_at: str
    last_heartbeat: str

    def to_dict(self) -> Dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict) -> 'InstanceInfo':
        return cls(**data)


class InstanceManager:
    """
    Manages instance isolation for multi-user/multi-instance scenarios.

    Features:
    - Unique instance IDs based on user + hostname + timestamp
    - Dynamic port allocation for dashboard (avoids conflicts)
    - Instance-specific file naming (logs, state, etc.)
    - Instance discovery and coordination
    - Stale instance cleanup
    """

    def __init__(self, project_path: str, port_range: tuple = (5050, 5150)):
        self.project_path = Path(project_path).resolve()
        self.port_range = port_range

        # Generate unique instance ID
        self.user = os.getenv("USER") or os.getenv("USERNAME") or "unknown"
        self.hostname = socket.gethostname()
        self.pid = os.getpid()
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Instance ID format: username_hostname_timestamp
        self.instance_id = f"{self.user}_{self.hostname}_{timestamp}"

        # Instance registry file (shared across all instances)
        self.instances_dir = self.project_path / ".claude_orchestra_instances"
        self.instances_dir.mkdir(exist_ok=True)

        self.instance_file = self.instances_dir / f"{self.instance_id}.json"
        self.lock_file = self.instances_dir / ".lock"

        # Instance-specific paths
        self._dashboard_port: Optional[int] = None

    def get_instance_prefix(self) -> str:
        """Get the prefix for instance-specific files."""
        # Use shorter prefix: user_hostname (no timestamp for readability)
        return f"{self.user}_{self.hostname}"

    def get_state_file(self) -> Path:
        """Get instance-specific state file path."""
        prefix = self.get_instance_prefix()
        return self.project_path / f".claude_orchestra_state_{prefix}.json"

    def get_log_file(self, log_type: str = "daemon") -> Path:
        """Get instance-specific log file path."""
        prefix = self.get_instance_prefix()
        return self.project_path / f"claude_orchestra_{log_type}_{prefix}.log"

    def get_stop_file(self) -> Path:
        """Get instance-specific stop file path."""
        prefix = self.get_instance_prefix()
        return self.project_path / f".claude_orchestra_stop_{prefix}"

    def get_summary_file(self) -> Path:
        """Get instance-specific summary file path."""
        prefix = self.get_instance_prefix()
        return self.project_path / f".claude_orchestra_summary_{prefix}.md"

    def allocate_port(self) -> int:
        """
        Allocate an available port for the dashboard.

        Returns:
            Port number that's currently available
        """
        if self._dashboard_port:
            return self._dashboard_port

        # Try to find an available port
        for port in range(self.port_range[0], self.port_range[1]):
            if self._is_port_available(port):
                self._dashboard_port = port
                return port

        # If no port found in range, let OS assign one
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(('', 0))
            s.listen(1)
            port = s.getsockname()[1]
            self._dashboard_port = port
            return port

    def _is_port_available(self, port: int) -> bool:
        """Check if a port is available."""
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(('0.0.0.0', port))
                return True
        except OSError:
            return False

    def register_instance(self, dashboard_port: Optional[int] = None) -> InstanceInfo:
        """
        Register this instance in the shared registry.

        Args:
            dashboard_port: Port the dashboard is running on (if applicable)

        Returns:
            InstanceInfo for this instance
        """
        info = InstanceInfo(
            instance_id=self.instance_id,
            user=self.user,
            hostname=self.hostname,
            project_path=str(self.project_path),
            pid=self.pid,
            dashboard_port=dashboard_port or self._dashboard_port,
            started_at=datetime.now().isoformat(),
            last_heartbeat=datetime.now().isoformat()
        )

        # Write to registry with file locking
        with self._acquire_lock():
            self.instance_file.write_text(json.dumps(info.to_dict(), indent=2))

        return info

    def update_heartbeat(self):
        """Update the heartbeat timestamp for this instance."""
        if not self.instance_file.exists():
            return

        with self._acquire_lock():
            try:
                data = json.loads(self.instance_file.read_text())
                data['last_heartbeat'] = datetime.now().isoformat()
                self.instance_file.write_text(json.dumps(data, indent=2))
            except Exception:
                pass  # Ignore heartbeat failures

    def unregister_instance(self):
        """Remove this instance from the registry."""
        with self._acquire_lock():
            if self.instance_file.exists():
                self.instance_file.unlink()

    def get_all_instances(self, active_only: bool = True) -> List[InstanceInfo]:
        """
        Get all registered instances.

        Args:
            active_only: If True, only return instances with recent heartbeats

        Returns:
            List of InstanceInfo objects
        """
        instances = []
        cutoff_time = datetime.now().timestamp() - 300  # 5 minutes

        with self._acquire_lock():
            for instance_file in self.instances_dir.glob("*.json"):
                if instance_file.name == ".lock":
                    continue

                try:
                    data = json.loads(instance_file.read_text())
                    info = InstanceInfo.from_dict(data)

                    if active_only:
                        # Check if instance is still active
                        heartbeat = datetime.fromisoformat(info.last_heartbeat)
                        if heartbeat.timestamp() < cutoff_time:
                            # Stale instance, skip it
                            continue

                        # Also check if process is still running
                        if not self._is_process_running(info.pid):
                            continue

                    instances.append(info)
                except Exception:
                    continue

        return instances

    def cleanup_stale_instances(self) -> int:
        """
        Clean up registry entries for instances that are no longer running.

        Returns:
            Number of stale instances cleaned up
        """
        cleaned = 0
        cutoff_time = datetime.now().timestamp() - 300  # 5 minutes

        with self._acquire_lock():
            for instance_file in self.instances_dir.glob("*.json"):
                if instance_file.name == ".lock":
                    continue

                try:
                    data = json.loads(instance_file.read_text())
                    info = InstanceInfo.from_dict(data)

                    # Check heartbeat
                    heartbeat = datetime.fromisoformat(info.last_heartbeat)
                    is_stale = heartbeat.timestamp() < cutoff_time

                    # Check if process exists
                    process_dead = not self._is_process_running(info.pid)

                    if is_stale or process_dead:
                        instance_file.unlink()
                        cleaned += 1
                except Exception:
                    # If we can't read it, it's probably corrupted - delete it
                    instance_file.unlink()
                    cleaned += 1

        return cleaned

    def _is_process_running(self, pid: int) -> bool:
        """Check if a process with the given PID is running."""
        try:
            os.kill(pid, 0)
            return True
        except (OSError, ProcessLookupError):
            return False

    def _acquire_lock(self):
        """Context manager for file-based locking."""
        return FileLock(self.lock_file)

    def get_dashboard_url(self) -> str:
        """Get the URL for this instance's dashboard."""
        port = self._dashboard_port or 5050
        return f"http://{self.hostname}:port"

    def print_instance_info(self):
        """Print information about this instance."""
        print(f"\n{'='*60}")
        print(f"Claude Orchestra Instance")
        print(f"{'='*60}")
        print(f"Instance ID:     {self.instance_id}")
        print(f"User:            {self.user}")
        print(f"Hostname:        {self.hostname}")
        print(f"PID:             {self.pid}")
        print(f"Project:         {self.project_path}")

        if self._dashboard_port:
            print(f"Dashboard:       http://localhost:{self._dashboard_port}")

        print(f"\nInstance Files:")
        print(f"  State:         {self.get_state_file().name}")
        print(f"  Logs:          {self.get_log_file().name}")
        print(f"  Summary:       {self.get_summary_file().name}")
        print(f"  Stop file:     {self.get_stop_file().name}")
        print(f"{'='*60}\n")

    def list_all_instances(self):
        """Print all active instances."""
        instances = self.get_all_instances(active_only=True)

        if not instances:
            print("No active instances found.")
            return

        print(f"\n{'='*80}")
        print(f"Active Claude Orchestra Instances ({len(instances)})")
        print(f"{'='*80}")

        for info in instances:
            is_current = info.instance_id == self.instance_id
            marker = " (THIS INSTANCE)" if is_current else ""

            print(f"\nInstance: {info.instance_id}{marker}")
            print(f"  User:      {info.user}@{info.hostname}")
            print(f"  PID:       {info.pid}")
            print(f"  Started:   {info.started_at}")
            print(f"  Heartbeat: {info.last_heartbeat}")

            if info.dashboard_port:
                print(f"  Dashboard: http://localhost:{info.dashboard_port}")

        print(f"{'='*80}\n")


class FileLock:
    """Simple file-based lock for coordination between processes."""

    def __init__(self, lock_file: Path):
        self.lock_file = lock_file
        self.lock_fd = None

    def __enter__(self):
        # Create lock file if it doesn't exist
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock_file.touch(exist_ok=True)

        # Acquire exclusive lock
        self.lock_fd = open(self.lock_file, 'r')
        fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.lock_fd:
            fcntl.flock(self.lock_fd.fileno(), fcntl.LOCK_UN)
            self.lock_fd.close()
