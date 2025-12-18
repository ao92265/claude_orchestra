#!/usr/bin/env python3
"""
Claude Orchestra Daemon - 24/7 Autonomous Development

A persistent development pipeline that runs continuously, saves state,
produces summaries, and can be stopped/resumed at any time.

Features:
- State persistence (resume after restart)
- Detailed session summaries
- Multiple stop conditions (cycles, time, stop file)
- Email/webhook notifications
- Comprehensive logging

Usage:
    # Start daemon mode (runs until stopped)
    python claude_orchestra_daemon.py --project /path/to/project --daemon

    # Run for 24 hours max
    python claude_orchestra_daemon.py --project /path/to/project --daemon --max-hours 24

    # Stop gracefully (create stop file)
    touch /path/to/project/.claude_orchestra_stop

    # View summary
    python claude_orchestra_daemon.py --project /path/to/project --summary
"""

import json
import logging
import time
import signal
import sys
import smtplib
import ssl
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path
from dataclasses import dataclass, asdict, field
from datetime import datetime, timedelta
from typing import Optional, List, Dict, Any
from enum import Enum
import argparse
import os

# Import the main orchestra
from claude_orchestra import ClaudeOrchestra, AgentRole, AgentResult

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


@dataclass
class CycleRecord:
    """Record of a single development cycle."""
    cycle_number: int
    started_at: str
    completed_at: Optional[str] = None
    duration_seconds: float = 0.0
    task_implemented: Optional[str] = None
    branch_name: Optional[str] = None
    pr_number: Optional[int] = None
    review_decision: Optional[str] = None
    review_iterations: int = 1
    tasks_added: List[str] = field(default_factory=list)
    success: bool = False
    error: Optional[str] = None


@dataclass
class DaemonState:
    """Persistent state for the daemon."""
    # Session info
    session_id: str = ""
    started_at: str = ""
    last_active: str = ""

    # Counters
    total_cycles: int = 0
    successful_cycles: int = 0
    failed_cycles: int = 0
    total_prs_created: int = 0
    total_prs_approved: int = 0
    total_tasks_added: int = 0

    # Current state
    is_running: bool = False
    current_cycle: int = 0
    last_branch: Optional[str] = None
    last_pr: Optional[int] = None

    # History (last N cycles)
    cycle_history: List[Dict] = field(default_factory=list)

    # Configuration
    max_cycles: int = 100
    max_hours: float = 0  # 0 = unlimited
    delay_between_cycles: int = 300  # 5 minutes


class StopReason(Enum):
    MAX_CYCLES = "max_cycles_reached"
    MAX_TIME = "max_time_reached"
    STOP_FILE = "stop_file_detected"
    USER_INTERRUPT = "user_interrupt"
    ERROR = "error"
    MANUAL = "manual"


@dataclass
class EmailConfig:
    """Email notification configuration."""
    enabled: bool = False
    smtp_server: str = "smtp.gmail.com"
    smtp_port: int = 587
    sender_email: str = ""
    sender_password: str = ""  # Use app password for Gmail
    recipient_email: str = ""
    # Notification preferences
    notify_on_cycle_complete: bool = True
    notify_on_failure: bool = True
    notify_on_pr_approved: bool = True
    notify_on_session_end: bool = True
    # Batch notifications (send digest instead of every cycle)
    batch_notifications: bool = False
    batch_interval_cycles: int = 5  # Send digest every N cycles


class EmailNotifier:
    """Handles email notifications for the daemon."""

    def __init__(self, config: EmailConfig, project_name: str = ""):
        self.config = config
        self.project_name = project_name
        self.pending_notifications: List[Dict] = []

    def _send_email(self, subject: str, body_html: str, body_text: str) -> bool:
        """Send an email notification."""
        if not self.config.enabled:
            return False

        if not all([self.config.sender_email, self.config.sender_password, self.config.recipient_email]):
            logger.warning("Email configuration incomplete, skipping notification")
            return False

        try:
            msg = MIMEMultipart("alternative")
            msg["Subject"] = f"🎭 Claude Orchestra: {subject}"
            msg["From"] = self.config.sender_email
            msg["To"] = self.config.recipient_email

            # Attach both plain text and HTML versions
            msg.attach(MIMEText(body_text, "plain"))
            msg.attach(MIMEText(body_html, "html"))

            # Create secure connection
            context = ssl.create_default_context()

            with smtplib.SMTP(self.config.smtp_server, self.config.smtp_port) as server:
                server.starttls(context=context)
                server.login(self.config.sender_email, self.config.sender_password)
                server.sendmail(
                    self.config.sender_email,
                    self.config.recipient_email,
                    msg.as_string()
                )

            logger.info(f"Email notification sent: {subject}")
            return True

        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False

    def notify_cycle_complete(self, record: 'CycleRecord', state: 'DaemonState'):
        """Send notification for cycle completion."""
        if not self.config.notify_on_cycle_complete:
            return

        if self.config.batch_notifications:
            self.pending_notifications.append(asdict(record))
            if len(self.pending_notifications) >= self.config.batch_interval_cycles:
                self._send_batch_digest(state)
            return

        status = "✅ SUCCESS" if record.success else "❌ FAILED"
        pr_info = f"PR #{record.pr_number}" if record.pr_number else "No PR"
        review_info = record.review_decision or "N/A"

        subject = f"Cycle {record.cycle_number} {status}"

        body_html = f"""
        <html>
        <body style="font-family: Arial, sans-serif; max-width: 600px; margin: 0 auto;">
            <h2 style="color: {'#28a745' if record.success else '#dc3545'};">
                {status} - Cycle {record.cycle_number}
            </h2>

            <table style="width: 100%; border-collapse: collapse;">
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Project</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{self.project_name}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Task</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{record.task_implemented or 'Unknown'}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Branch</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;"><code>{record.branch_name or 'N/A'}</code></td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>PR</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{pr_info}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Review</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{review_info}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Review Iterations</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{record.review_iterations}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Duration</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{record.duration_seconds:.1f}s</td></tr>
            </table>

            <h3>Session Progress</h3>
            <p>
                Total: {state.total_cycles} cycles |
                Success: {state.successful_cycles} |
                Failed: {state.failed_cycles} |
                PRs Approved: {state.total_prs_approved}
            </p>

            {f'<p style="color: #dc3545;"><strong>Error:</strong> {record.error}</p>' if record.error else ''}
        </body>
        </html>
        """

        body_text = f"""
Cycle {record.cycle_number} {status}

Project: {self.project_name}
Task: {record.task_implemented or 'Unknown'}
Branch: {record.branch_name or 'N/A'}
PR: {pr_info}
Review: {review_info}
Duration: {record.duration_seconds:.1f}s

Session Progress: {state.total_cycles} cycles, {state.successful_cycles} successful
        """

        self._send_email(subject, body_html, body_text)

    def _send_batch_digest(self, state: 'DaemonState'):
        """Send a digest of multiple cycles."""
        if not self.pending_notifications:
            return

        successful = sum(1 for n in self.pending_notifications if n.get('success'))
        failed = len(self.pending_notifications) - successful

        subject = f"Digest: {len(self.pending_notifications)} cycles ({successful} ✅, {failed} ❌)"

        cycles_html = ""
        for n in self.pending_notifications:
            status = "✅" if n.get('success') else "❌"
            cycles_html += f"""
            <tr>
                <td style="padding: 4px;">{n.get('cycle_number')}</td>
                <td style="padding: 4px;">{status}</td>
                <td style="padding: 4px;">{n.get('task_implemented', 'Unknown')[:50]}</td>
                <td style="padding: 4px;">#{n.get('pr_number', 'N/A')}</td>
                <td style="padding: 4px;">{n.get('review_decision', 'N/A')}</td>
            </tr>
            """

        body_html = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2>Claude Orchestra Digest</h2>
            <p><strong>Project:</strong> {self.project_name}</p>
            <p><strong>Cycles:</strong> {len(self.pending_notifications)} ({successful} successful, {failed} failed)</p>

            <table style="width: 100%; border-collapse: collapse; margin-top: 20px;">
                <tr style="background: #f5f5f5;">
                    <th style="padding: 8px; text-align: left;">Cycle</th>
                    <th style="padding: 8px; text-align: left;">Status</th>
                    <th style="padding: 8px; text-align: left;">Task</th>
                    <th style="padding: 8px; text-align: left;">PR</th>
                    <th style="padding: 8px; text-align: left;">Review</th>
                </tr>
                {cycles_html}
            </table>

            <h3>Session Totals</h3>
            <p>
                Total: {state.total_cycles} |
                Successful: {state.successful_cycles} |
                PRs Approved: {state.total_prs_approved}
            </p>
        </body>
        </html>
        """

        body_text = f"Claude Orchestra Digest: {len(self.pending_notifications)} cycles completed"

        self._send_email(subject, body_html, body_text)
        self.pending_notifications = []

    def notify_pr_approved(self, pr_number: int, branch_name: str):
        """Send notification when PR is approved."""
        if not self.config.notify_on_pr_approved:
            return

        subject = f"PR #{pr_number} Approved! 🎉"

        body_html = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2 style="color: #28a745;">🎉 PR Approved!</h2>
            <p><strong>Project:</strong> {self.project_name}</p>
            <p><strong>PR:</strong> #{pr_number}</p>
            <p><strong>Branch:</strong> <code>{branch_name}</code></p>
            <p>The code review passed and the PR is ready to merge.</p>
        </body>
        </html>
        """

        body_text = f"PR #{pr_number} on branch {branch_name} has been approved!"

        self._send_email(subject, body_html, body_text)

    def notify_failure(self, cycle_number: int, error: str):
        """Send notification on cycle failure."""
        if not self.config.notify_on_failure:
            return

        subject = f"Cycle {cycle_number} Failed ⚠️"

        body_html = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2 style="color: #dc3545;">⚠️ Cycle Failed</h2>
            <p><strong>Project:</strong> {self.project_name}</p>
            <p><strong>Cycle:</strong> {cycle_number}</p>
            <p><strong>Error:</strong></p>
            <pre style="background: #f5f5f5; padding: 10px; overflow-x: auto;">{error}</pre>
        </body>
        </html>
        """

        body_text = f"Cycle {cycle_number} failed: {error}"

        self._send_email(subject, body_html, body_text)

    def notify_session_end(self, state: 'DaemonState', reason: StopReason):
        """Send notification when daemon session ends."""
        if not self.config.notify_on_session_end:
            return

        # Send any pending batch notifications first
        if self.pending_notifications:
            self._send_batch_digest(state)

        success_rate = (state.successful_cycles / state.total_cycles * 100) if state.total_cycles > 0 else 0

        subject = f"Session Ended: {state.total_cycles} cycles completed"

        body_html = f"""
        <html>
        <body style="font-family: Arial, sans-serif;">
            <h2>🏁 Claude Orchestra Session Ended</h2>

            <p><strong>Project:</strong> {self.project_name}</p>
            <p><strong>Reason:</strong> {reason.value}</p>

            <h3>Final Statistics</h3>
            <table style="border-collapse: collapse;">
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Total Cycles</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{state.total_cycles}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Successful</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{state.successful_cycles}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Failed</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{state.failed_cycles}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Success Rate</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{success_rate:.1f}%</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>PRs Created</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{state.total_prs_created}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>PRs Approved</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{state.total_prs_approved}</td></tr>
                <tr><td style="padding: 8px; border-bottom: 1px solid #ddd;"><strong>Tasks Added</strong></td>
                    <td style="padding: 8px; border-bottom: 1px solid #ddd;">{state.total_tasks_added}</td></tr>
            </table>

            <p style="margin-top: 20px;">
                To resume: <code>python claude_orchestra_daemon.py --project {self.project_name} --daemon</code>
            </p>
        </body>
        </html>
        """

        body_text = f"""
Claude Orchestra Session Ended

Project: {self.project_name}
Reason: {reason.value}

Statistics:
- Total Cycles: {state.total_cycles}
- Successful: {state.successful_cycles}
- Failed: {state.failed_cycles}
- Success Rate: {success_rate:.1f}%
- PRs Created: {state.total_prs_created}
- PRs Approved: {state.total_prs_approved}
        """

        self._send_email(subject, body_html, body_text)


class ClaudeOrchestraDaemon:
    """
    24/7 Autonomous Development Daemon

    Runs development cycles continuously with:
    - State persistence for resume capability
    - Detailed logging and summaries
    - Configurable stop conditions
    - Graceful shutdown handling
    """

    def __init__(
        self,
        project_path: str,
        max_cycles: int = 100,
        max_hours: float = 0,
        delay_between_cycles: int = 300,
        model: str = "sonnet",
        email_config: Optional[EmailConfig] = None
    ):
        self.project_path = Path(project_path).resolve()
        self.model = model

        # File paths
        self.state_file = self.project_path / ".claude_orchestra_state.json"
        self.summary_file = self.project_path / ".claude_orchestra_summary.md"
        self.stop_file = self.project_path / ".claude_orchestra_stop"
        self.log_file = self.project_path / "claude_orchestra_daemon.log"
        self.email_config_file = self.project_path / ".claude_orchestra_email.json"

        # Set up file logging
        file_handler = logging.FileHandler(self.log_file)
        file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
        logger.addHandler(file_handler)

        # Initialize or load state
        self.state = self._load_state()
        self.state.max_cycles = max_cycles
        self.state.max_hours = max_hours
        self.state.delay_between_cycles = delay_between_cycles

        # Initialize orchestra
        self.orchestra = ClaudeOrchestra(
            project_path=str(self.project_path),
            model=model
        )

        # Initialize email notifications
        self.email_config = email_config or self._load_email_config()
        self.notifier = EmailNotifier(
            config=self.email_config,
            project_name=self.project_path.name
        )

        # Track session
        self.session_start = datetime.now()
        self.should_stop = False
        self.stop_reason: Optional[StopReason] = None

        # Set up signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        logger.info(f"Daemon initialized for: {self.project_path}")
        if self.email_config.enabled:
            logger.info(f"Email notifications enabled: {self.email_config.recipient_email}")

    def _handle_signal(self, signum, frame):
        """Handle shutdown signals gracefully."""
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self.should_stop = True
        self.stop_reason = StopReason.USER_INTERRUPT

    def _load_state(self) -> DaemonState:
        """Load state from disk or create new."""
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                # Convert cycle_history dicts back
                state = DaemonState(**{k: v for k, v in data.items() if k != 'cycle_history'})
                state.cycle_history = data.get('cycle_history', [])
                logger.info(f"Loaded existing state: {state.total_cycles} cycles completed")
                return state
            except Exception as e:
                logger.warning(f"Failed to load state: {e}, starting fresh")

        # New session
        state = DaemonState()
        state.session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
        state.started_at = datetime.now().isoformat()
        return state

    def _save_state(self):
        """Persist state to disk."""
        self.state.last_active = datetime.now().isoformat()
        try:
            self.state_file.write_text(json.dumps(asdict(self.state), indent=2, default=str))
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    def _load_email_config(self) -> EmailConfig:
        """Load email configuration from file or environment variables."""
        # Try loading from config file first
        if self.email_config_file.exists():
            try:
                data = json.loads(self.email_config_file.read_text())
                return EmailConfig(**data)
            except Exception as e:
                logger.warning(f"Failed to load email config: {e}")

        # Fall back to environment variables
        config = EmailConfig(
            enabled=os.getenv("CLAUDE_ORCHESTRA_EMAIL_ENABLED", "").lower() == "true",
            smtp_server=os.getenv("CLAUDE_ORCHESTRA_SMTP_SERVER", "smtp.gmail.com"),
            smtp_port=int(os.getenv("CLAUDE_ORCHESTRA_SMTP_PORT", "587")),
            sender_email=os.getenv("CLAUDE_ORCHESTRA_SENDER_EMAIL", ""),
            sender_password=os.getenv("CLAUDE_ORCHESTRA_SENDER_PASSWORD", ""),
            recipient_email=os.getenv("CLAUDE_ORCHESTRA_RECIPIENT_EMAIL", ""),
            batch_notifications=os.getenv("CLAUDE_ORCHESTRA_BATCH_EMAILS", "").lower() == "true",
            batch_interval_cycles=int(os.getenv("CLAUDE_ORCHESTRA_BATCH_INTERVAL", "5"))
        )

        return config

    def save_email_config(self, config: EmailConfig):
        """Save email configuration to file (without password for security)."""
        # Don't save password to file - use env var for that
        config_dict = asdict(config)
        config_dict['sender_password'] = ""  # Clear password
        self.email_config_file.write_text(json.dumps(config_dict, indent=2))
        logger.info(f"Email config saved to {self.email_config_file}")
        logger.info("Note: Set CLAUDE_ORCHESTRA_SENDER_PASSWORD env var for the password")

    def _check_stop_conditions(self) -> Optional[StopReason]:
        """Check if any stop condition is met."""
        # Check stop file
        if self.stop_file.exists():
            logger.info("Stop file detected")
            self.stop_file.unlink()  # Remove the stop file
            return StopReason.STOP_FILE

        # Check max cycles
        if self.state.max_cycles > 0 and self.state.total_cycles >= self.state.max_cycles:
            logger.info(f"Max cycles ({self.state.max_cycles}) reached")
            return StopReason.MAX_CYCLES

        # Check max time
        if self.state.max_hours > 0:
            elapsed = datetime.now() - self.session_start
            if elapsed.total_seconds() >= self.state.max_hours * 3600:
                logger.info(f"Max time ({self.state.max_hours}h) reached")
                return StopReason.MAX_TIME

        # Check interrupt flag
        if self.should_stop:
            return self.stop_reason or StopReason.USER_INTERRUPT

        return None

    def _record_cycle(self, cycle_num: int, results: Dict[str, Any]) -> CycleRecord:
        """Create a record of a completed cycle."""
        record = CycleRecord(
            cycle_number=cycle_num,
            started_at=datetime.now().isoformat()
        )

        # Extract info from results
        if 'implementer' in results:
            impl = results['implementer']
            record.branch_name = impl.branch_name
            # Try to extract task from output
            if impl.output and "TASK_COMPLETED:" in impl.output:
                for line in impl.output.split('\n'):
                    if "TASK_COMPLETED:" in line:
                        record.task_implemented = line.split(":", 1)[1].strip()[:100]
                        break

        if 'tester' in results:
            record.pr_number = results['tester'].pr_number

        if 'reviewer' in results:
            record.review_decision = results['reviewer'].review_decision

        if 'total_review_iterations' in results:
            record.review_iterations = results['total_review_iterations']

        if 'planner' in results:
            planner = results['planner']
            if planner.output and "TASKS_ADDED:" in planner.output:
                for line in planner.output.split('\n'):
                    if "TASKS_ADDED:" in line:
                        tasks_str = line.split(":", 1)[1].strip()
                        record.tasks_added = [t.strip() for t in tasks_str.split(",")][:5]
                        break

        # Determine success
        record.success = all(
            r.success for r in results.values()
            if isinstance(r, AgentResult)
        )

        record.completed_at = datetime.now().isoformat()

        return record

    def _update_summary(self):
        """Generate and save a markdown summary."""
        state = self.state
        elapsed = datetime.now() - datetime.fromisoformat(state.started_at) if state.started_at else timedelta(0)

        summary = f"""# Claude Orchestra Session Summary

## Session Info
- **Session ID:** {state.session_id}
- **Started:** {state.started_at}
- **Last Active:** {state.last_active}
- **Running Time:** {str(elapsed).split('.')[0]}
- **Status:** {'🟢 Running' if state.is_running else '🔴 Stopped'}

## Statistics
| Metric | Value |
|--------|-------|
| Total Cycles | {state.total_cycles} |
| Successful Cycles | {state.successful_cycles} |
| Failed Cycles | {state.failed_cycles} |
| Success Rate | {(state.successful_cycles / state.total_cycles * 100) if state.total_cycles > 0 else 0:.1f}% |
| PRs Created | {state.total_prs_created} |
| PRs Approved | {state.total_prs_approved} |
| Tasks Added | {state.total_tasks_added} |

## Recent Cycles
"""

        # Add recent cycle history
        for record in state.cycle_history[-10:]:  # Last 10 cycles
            status = "✅" if record.get('success') else "❌"
            pr_info = f"PR #{record.get('pr_number')}" if record.get('pr_number') else "No PR"
            review = record.get('review_decision', 'N/A')

            summary += f"""
### Cycle {record.get('cycle_number')} {status}
- **Task:** {record.get('task_implemented', 'Unknown')[:80]}
- **Branch:** `{record.get('branch_name', 'N/A')}`
- **PR:** {pr_info} ({review})
- **Review Iterations:** {record.get('review_iterations', 1)}
- **Completed:** {record.get('completed_at', 'N/A')}
"""

        # Add stop info if stopped
        if not state.is_running and self.stop_reason:
            summary += f"""
## Session Ended
- **Reason:** {self.stop_reason.value}
- **Final Cycle:** {state.current_cycle}

To resume, run:
```bash
python claude_orchestra_daemon.py --project {self.project_path} --daemon
```
"""

        self.summary_file.write_text(summary)
        logger.info(f"Summary updated: {self.summary_file}")

    def run_daemon(self):
        """Run the daemon loop."""
        logger.info("=" * 60)
        logger.info("Starting Claude Orchestra Daemon")
        logger.info(f"Max cycles: {self.state.max_cycles}")
        logger.info(f"Max hours: {self.state.max_hours or 'unlimited'}")
        logger.info(f"Delay between cycles: {self.state.delay_between_cycles}s")
        logger.info("=" * 60)
        logger.info(f"To stop gracefully: touch {self.stop_file}")

        self.state.is_running = True
        self._save_state()

        try:
            while True:
                # Check stop conditions
                stop_reason = self._check_stop_conditions()
                if stop_reason:
                    self.stop_reason = stop_reason
                    break

                # Run a cycle
                self.state.current_cycle = self.state.total_cycles + 1
                logger.info(f"\n{'#' * 60}")
                logger.info(f"CYCLE {self.state.current_cycle}")
                logger.info(f"{'#' * 60}")

                cycle_start = time.time()

                try:
                    results = self.orchestra.run_full_cycle(max_review_iterations=3)

                    # Record the cycle
                    record = self._record_cycle(self.state.current_cycle, results)
                    record.duration_seconds = time.time() - cycle_start

                    # Update state
                    self.state.total_cycles += 1
                    if record.success:
                        self.state.successful_cycles += 1
                    else:
                        self.state.failed_cycles += 1

                    if record.pr_number:
                        self.state.total_prs_created += 1
                        self.state.last_pr = record.pr_number

                    if record.review_decision == "APPROVED":
                        self.state.total_prs_approved += 1
                        # Send PR approved notification
                        self.notifier.notify_pr_approved(
                            pr_number=record.pr_number,
                            branch_name=record.branch_name or "unknown"
                        )

                    if record.tasks_added:
                        self.state.total_tasks_added += len(record.tasks_added)

                    if record.branch_name:
                        self.state.last_branch = record.branch_name

                    # Add to history (keep last 50)
                    self.state.cycle_history.append(asdict(record))
                    self.state.cycle_history = self.state.cycle_history[-50:]

                    self._save_state()
                    self._update_summary()

                    logger.info(f"Cycle {self.state.current_cycle} complete: {'SUCCESS' if record.success else 'FAILED'}")

                    # Send cycle completion notification
                    self.notifier.notify_cycle_complete(record, self.state)

                except Exception as e:
                    logger.error(f"Cycle {self.state.current_cycle} error: {e}")
                    self.state.failed_cycles += 1
                    self.state.total_cycles += 1
                    self._save_state()

                    # Send failure notification
                    self.notifier.notify_failure(self.state.current_cycle, str(e))

                # Check stop conditions again before sleeping
                stop_reason = self._check_stop_conditions()
                if stop_reason:
                    self.stop_reason = stop_reason
                    break

                # Wait before next cycle
                logger.info(f"Waiting {self.state.delay_between_cycles}s before next cycle...")
                logger.info(f"To stop: touch {self.stop_file}")

                # Sleep in smaller chunks to check for stop conditions
                for _ in range(self.state.delay_between_cycles // 10):
                    if self._check_stop_conditions():
                        break
                    time.sleep(10)

        except Exception as e:
            logger.error(f"Daemon error: {e}")
            self.stop_reason = StopReason.ERROR

        finally:
            self.state.is_running = False
            self._save_state()
            self._update_summary()

            # Send session end notification
            if self.stop_reason:
                self.notifier.notify_session_end(self.state, self.stop_reason)

            logger.info("\n" + "=" * 60)
            logger.info("DAEMON STOPPED")
            logger.info(f"Reason: {self.stop_reason.value if self.stop_reason else 'unknown'}")
            logger.info(f"Total cycles: {self.state.total_cycles}")
            logger.info(f"Successful: {self.state.successful_cycles}")
            logger.info(f"Failed: {self.state.failed_cycles}")
            logger.info(f"Summary: {self.summary_file}")
            logger.info("=" * 60)

    def show_summary(self):
        """Display the current summary."""
        self._update_summary()
        print(self.summary_file.read_text())

    def reset_state(self):
        """Reset all state (use with caution)."""
        if self.state_file.exists():
            self.state_file.unlink()
        if self.summary_file.exists():
            self.summary_file.unlink()
        logger.info("State reset complete")


def main():
    parser = argparse.ArgumentParser(
        description="Claude Orchestra Daemon - 24/7 Autonomous Development",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Start daemon (runs until stopped)
    python claude_orchestra_daemon.py --project /path/to/project --daemon

    # Run for max 50 cycles
    python claude_orchestra_daemon.py --project /path/to/project --daemon --max-cycles 50

    # Run for max 24 hours
    python claude_orchestra_daemon.py --project /path/to/project --daemon --max-hours 24

    # Stop gracefully (from another terminal)
    touch /path/to/project/.claude_orchestra_stop

    # View summary
    python claude_orchestra_daemon.py --project /path/to/project --summary

    # Reset state (start fresh)
    python claude_orchestra_daemon.py --project /path/to/project --reset

    # Setup email notifications
    python claude_orchestra_daemon.py --project /path/to/project --setup-email \\
        --email-recipient your@email.com \\
        --email-sender bot@gmail.com

    # Then set password via environment variable:
    export CLAUDE_ORCHESTRA_SENDER_PASSWORD="your-app-password"
        """
    )

    parser.add_argument("--project", "-P", required=True, help="Project directory")
    parser.add_argument("--daemon", action="store_true", help="Run in daemon mode")
    parser.add_argument("--summary", action="store_true", help="Show session summary")
    parser.add_argument("--reset", action="store_true", help="Reset all state")
    parser.add_argument("--max-cycles", type=int, default=100, help="Max cycles (default: 100)")
    parser.add_argument("--max-hours", type=float, default=0, help="Max hours (0=unlimited)")
    parser.add_argument("--delay", type=int, default=300, help="Delay between cycles in seconds (default: 300)")
    parser.add_argument("--model", default="sonnet", help="Claude model")

    # Email configuration
    parser.add_argument("--setup-email", action="store_true", help="Setup email notifications")
    parser.add_argument("--email-recipient", help="Email address to receive notifications")
    parser.add_argument("--email-sender", help="Email address to send from (e.g., Gmail)")
    parser.add_argument("--smtp-server", default="smtp.gmail.com", help="SMTP server (default: smtp.gmail.com)")
    parser.add_argument("--smtp-port", type=int, default=587, help="SMTP port (default: 587)")
    parser.add_argument("--batch-emails", action="store_true", help="Send digest every N cycles instead of per-cycle")
    parser.add_argument("--batch-interval", type=int, default=5, help="Cycles between digest emails (default: 5)")
    parser.add_argument("--test-email", action="store_true", help="Send a test email to verify configuration")

    args = parser.parse_args()

    # Handle email setup first (before daemon init for test-email)
    if args.setup_email:
        config = EmailConfig(
            enabled=True,
            smtp_server=args.smtp_server,
            smtp_port=args.smtp_port,
            sender_email=args.email_sender or "",
            sender_password="",  # Set via env var
            recipient_email=args.email_recipient or "",
            batch_notifications=args.batch_emails,
            batch_interval_cycles=args.batch_interval
        )

        # Save config
        config_file = Path(args.project) / ".claude_orchestra_email.json"
        config_dict = asdict(config)
        config_dict['sender_password'] = ""  # Don't save password
        config_file.write_text(json.dumps(config_dict, indent=2))

        print(f"✅ Email configuration saved to {config_file}")
        print(f"   Recipient: {config.recipient_email}")
        print(f"   Sender: {config.sender_email}")
        print(f"   SMTP: {config.smtp_server}:{config.smtp_port}")
        print(f"   Batch mode: {'Yes' if config.batch_notifications else 'No'}")
        print()
        print("⚠️  Set the sender password via environment variable:")
        print("   export CLAUDE_ORCHESTRA_SENDER_PASSWORD='your-app-password'")
        print()
        print("   For Gmail, create an App Password at:")
        print("   https://myaccount.google.com/apppasswords")
        return

    daemon = ClaudeOrchestraDaemon(
        project_path=args.project,
        max_cycles=args.max_cycles,
        max_hours=args.max_hours,
        delay_between_cycles=args.delay,
        model=args.model
    )

    if args.test_email:
        # Send a test email
        if not daemon.email_config.enabled:
            print("❌ Email not configured. Run --setup-email first.")
            return

        print(f"Sending test email to {daemon.email_config.recipient_email}...")
        test_record = CycleRecord(
            cycle_number=0,
            started_at=datetime.now().isoformat(),
            completed_at=datetime.now().isoformat(),
            task_implemented="Test notification",
            success=True
        )
        daemon.notifier.notify_cycle_complete(test_record, daemon.state)
        print("✅ Test email sent! Check your inbox.")
    elif args.reset:
        daemon.reset_state()
    elif args.summary:
        daemon.show_summary()
    elif args.daemon:
        daemon.run_daemon()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
