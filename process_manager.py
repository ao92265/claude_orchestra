#!/usr/bin/env python3
"""
Process Manager for Claude Orchestra
Handles process lifecycle, cleanup, and orphan detection
"""

import os
import signal
import subprocess
import logging
import time
import psutil
from typing import Dict, Set, Optional
from threading import Lock

logger = logging.getLogger(__name__)


class ProcessManager:
    """
    Manages spawned processes with automatic cleanup and orphan detection.

    Features:
    - Tracks all spawned subprocess PIDs
    - Registers signal handlers for graceful shutdown
    - Cleans up child processes on exit
    - Detects and kills orphaned processes
    - Thread-safe process tracking
    """

    def __init__(self):
        self._tracked_processes: Dict[str, subprocess.Popen] = {}
        self._tracked_pids: Set[int] = set()
        self._lock = Lock()
        self._shutdown_initiated = False

        # Register signal handlers
        signal.signal(signal.SIGTERM, self._handle_shutdown_signal)
        signal.signal(signal.SIGINT, self._handle_shutdown_signal)

        logger.info("ProcessManager initialized with signal handlers")

    def track_process(self, process_id: str, process: subprocess.Popen) -> None:
        """
        Register a process for tracking and automatic cleanup.

        Args:
            process_id: Unique identifier for the process (e.g., project name)
            process: The subprocess.Popen object to track
        """
        with self._lock:
            if process.pid:
                self._tracked_processes[process_id] = process
                self._tracked_pids.add(process.pid)
                logger.info(f"Tracking process {process_id} with PID {process.pid}")

    def untrack_process(self, process_id: str) -> None:
        """
        Remove a process from tracking (e.g., when it exits normally).

        Args:
            process_id: The process identifier to stop tracking
        """
        with self._lock:
            if process_id in self._tracked_processes:
                process = self._tracked_processes[process_id]
                if process.pid:
                    self._tracked_pids.discard(process.pid)
                del self._tracked_processes[process_id]
                logger.info(f"Stopped tracking process {process_id}")

    def get_process(self, process_id: str) -> Optional[subprocess.Popen]:
        """Get a tracked process by ID."""
        with self._lock:
            return self._tracked_processes.get(process_id)

    def is_running(self, process_id: str) -> bool:
        """Check if a tracked process is still running."""
        with self._lock:
            process = self._tracked_processes.get(process_id)
            if process:
                return process.poll() is None
            return False

    def stop_process(self, process_id: str, timeout: int = 10) -> bool:
        """
        Gracefully stop a tracked process with timeout.

        Args:
            process_id: The process to stop
            timeout: Seconds to wait before force kill

        Returns:
            True if process stopped successfully, False otherwise
        """
        with self._lock:
            process = self._tracked_processes.get(process_id)
            if not process:
                logger.warning(f"Process {process_id} not found for stopping")
                return False

            if process.poll() is not None:
                logger.info(f"Process {process_id} already exited")
                self.untrack_process(process_id)
                return True

        # Attempt graceful shutdown
        logger.info(f"Terminating process {process_id} (PID {process.pid})")
        try:
            process.terminate()
            process.wait(timeout=timeout)
            logger.info(f"Process {process_id} terminated gracefully")
            self.untrack_process(process_id)
            return True
        except subprocess.TimeoutExpired:
            # Force kill if graceful shutdown fails
            logger.warning(f"Process {process_id} did not terminate, force killing")
            process.kill()
            process.wait(timeout=5)
            self.untrack_process(process_id)
            return True
        except Exception as e:
            logger.error(f"Error stopping process {process_id}: {e}")
            return False

    def stop_all_processes(self, timeout: int = 10) -> None:
        """
        Stop all tracked processes gracefully.

        Args:
            timeout: Seconds to wait for each process before force kill
        """
        logger.info("Stopping all tracked processes")

        with self._lock:
            process_ids = list(self._tracked_processes.keys())

        for process_id in process_ids:
            self.stop_process(process_id, timeout=timeout)

        logger.info("All tracked processes stopped")

    def detect_and_kill_orphans(self, project_path: Optional[str] = None) -> int:
        """
        Detect and kill orphaned Claude processes.

        Orphaned processes are those that:
        1. Match the Claude CLI command pattern
        2. Are no longer tracked by this manager
        3. (Optional) Are running in the specified project path

        Args:
            project_path: If provided, only kill orphans in this directory

        Returns:
            Number of orphaned processes killed
        """
        killed_count = 0

        try:
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'cwd']):
                try:
                    # Check if this looks like a Claude CLI process
                    cmdline = proc.info.get('cmdline', [])
                    if not cmdline:
                        continue

                    # Look for python processes running claude_orchestra
                    cmdline_str = ' '.join(cmdline)
                    is_orchestra = 'claude_orchestra' in cmdline_str and 'python' in cmdline_str.lower()

                    # Look for actual Claude CLI processes
                    is_claude_cli = 'claude' in proc.info.get('name', '').lower() and '--dangerously' in cmdline_str

                    if not (is_orchestra or is_claude_cli):
                        continue

                    # Check if it's not tracked (orphaned)
                    pid = proc.info['pid']
                    with self._lock:
                        if pid in self._tracked_pids:
                            continue  # Still tracked, not an orphan

                    # Check project path if specified
                    if project_path:
                        proc_cwd = proc.info.get('cwd')
                        if proc_cwd != project_path:
                            continue

                    # This is an orphan - kill it
                    logger.warning(f"Killing orphaned process PID {pid}: {' '.join(cmdline[:3])}")
                    proc.terminate()

                    # Wait for graceful exit
                    proc.wait(timeout=5)
                    killed_count += 1

                except psutil.NoSuchProcess:
                    # Process already exited
                    pass
                except psutil.TimeoutExpired:
                    # Force kill if needed
                    try:
                        proc.kill()
                        killed_count += 1
                    except:
                        pass
                except Exception as e:
                    logger.error(f"Error checking process: {e}")

        except Exception as e:
            logger.error(f"Error during orphan detection: {e}")

        if killed_count > 0:
            logger.info(f"Killed {killed_count} orphaned process(es)")

        return killed_count

    def _handle_shutdown_signal(self, signum, frame):
        """Handle shutdown signals (SIGTERM, SIGINT)."""
        if self._shutdown_initiated:
            logger.warning("Shutdown already in progress")
            return

        self._shutdown_initiated = True
        signal_name = signal.Signals(signum).name
        logger.info(f"Received {signal_name}, initiating graceful shutdown")

        # Stop all tracked processes
        self.stop_all_processes(timeout=10)

        # Exit the application
        os._exit(0)

    def get_tracked_count(self) -> int:
        """Return the number of currently tracked processes."""
        with self._lock:
            return len(self._tracked_processes)

    def get_tracked_process_ids(self) -> list:
        """Return list of all tracked process IDs."""
        with self._lock:
            return list(self._tracked_processes.keys())


# Global singleton instance
_process_manager: Optional[ProcessManager] = None


def get_process_manager() -> ProcessManager:
    """Get the global ProcessManager singleton instance."""
    global _process_manager
    if _process_manager is None:
        _process_manager = ProcessManager()
    return _process_manager
