#!/usr/bin/env python3
"""
Claude Orchestra Dashboard - Web GUI for monitoring multi-agent pipeline
"""

import os
import json
import re
import subprocess
import threading
import time
import atexit
from datetime import datetime, timedelta
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO, emit
from process_manager import get_process_manager

# Multi-user mode support
try:
    from dashboard_claims import register_claims_handlers, get_multiuser_html_components
    MULTIUSER_AVAILABLE = True
except ImportError:
    MULTIUSER_AVAILABLE = False

app = Flask(__name__)
app.config['SECRET_KEY'] = 'claude-orchestra-secret'
socketio = SocketIO(app, cors_allowed_origins="*")

# Register multi-user handlers if available
if MULTIUSER_AVAILABLE:
    register_claims_handlers(socketio, app)

# Initialize process manager for cleanup
process_manager = get_process_manager()

# Register cleanup on exit
def cleanup_on_exit():
    """Clean up all processes when dashboard exits."""
    process_manager.stop_all_processes(timeout=5)
    orphan_count = process_manager.detect_and_kill_orphans()
    if orphan_count > 0:
        print(f"Cleaned up {orphan_count} orphaned process(es) on exit")

atexit.register(cleanup_on_exit)

# Default state template for a project
def create_project_state():
    return {
        "running": False,
        "project_id": None,
        "project_path": None,
        "current_cycle": 0,
        "current_stage": None,
        "start_time": None,
        "max_hours": None,
        "cycles_completed": 0,
        "prs_created": [],
        "log_lines": [],
        "process": None,
        # Activity tracking
        "branches_created": 0,
        "current_branch": None,
        "files_changed": 0,
        "files_changed_set": set(),  # Track unique files
        "last_file": None,
        "subagent_count": 0,
        "active_subagent": None,
        "subagents_used": [],  # Track all sub-agents used
        "tools_used": 0,
        "last_tool": None,
        "activity_log": []  # Log of all activities
    }

# Global state - now supports multiple projects
# Key: project_id (short name), Value: project state dict
projects_state = {}

# For backwards compatibility, also maintain single project reference
orchestra_state = create_project_state()

# Active project ID for UI focus
active_project_id = None

# Global usage tracking (across all projects)
usage_stats = {
    "requests_today": 0,
    "requests_this_week": 0,
    "tokens_estimated": 0,
    "last_reset_daily": None,
    "last_reset_weekly": None,
    "rate_limited": False,
    "rate_limit_until": None,
    "history": []  # [{timestamp, requests, tokens}, ...]
}

# Message queue for queued tasks
message_queue = []  # [{id, message, project_id, status, created_at}, ...]
queue_counter = 0

# Summary data for time-based view
summary_data = {
    "hourly": {},  # {hour_key: {prs: n, tasks: n, files: n}}
    "daily": {},   # {date_key: {prs: n, tasks: n, files: n}}
    "events": []   # [{timestamp, type, description, project_id}, ...]
}

# Safeguards configuration
safeguards = {
    "subagent_timeout_minutes": 30,  # Max time for a sub-agent before warning
    "known_repos": [],  # List of known repo paths to detect cross-repo activity
    "alerts": [],  # [{timestamp, type, message, project_id, severity}, ...]
    "path_violations": [],  # [{timestamp, attempted_path, project_path, project_id}, ...]
}

def init_known_repos():
    """Initialize list of known repos for cross-repo detection."""
    global safeguards
    repos_path = Path(os.path.expanduser("~/Repos"))
    if repos_path.exists():
        safeguards["known_repos"] = [str(p) for p in repos_path.iterdir() if p.is_dir() and (p / ".git").exists()]

def add_safeguard_alert(alert_type, message, project_id=None, severity="warning"):
    """Add a safeguard alert."""
    global safeguards
    alert = {
        "timestamp": datetime.now().isoformat(),
        "type": alert_type,
        "message": message,
        "project_id": project_id,
        "severity": severity  # "info", "warning", "critical"
    }
    safeguards["alerts"].append(alert)
    # Keep last 100 alerts
    if len(safeguards["alerts"]) > 100:
        safeguards["alerts"] = safeguards["alerts"][-100:]
    # Emit alert to dashboard
    socketio.emit('safeguard_alert', alert)
    # Also log to regular log
    socketio.emit('log_line', {'line': f'[SAFEGUARD:{severity.upper()}] {message}', 'project_id': project_id})
    return alert

def check_path_traversal(file_path, project_path, project_id):
    """Check if a file operation is outside the project directory."""
    if not file_path or not project_path:
        return False

    try:
        # Resolve both paths to absolute
        file_abs = Path(file_path).resolve()
        project_abs = Path(project_path).resolve()

        # Check if file is within project
        try:
            file_abs.relative_to(project_abs)
            return False  # File is within project - no violation
        except ValueError:
            # File is outside project directory
            violation = {
                "timestamp": datetime.now().isoformat(),
                "attempted_path": str(file_abs),
                "project_path": str(project_abs),
                "project_id": project_id
            }
            safeguards["path_violations"].append(violation)
            add_safeguard_alert(
                "path_traversal",
                f"Agent attempted to modify file outside project: {file_path}",
                project_id,
                "critical"
            )
            return True
    except Exception:
        return False

def check_cross_repo_activity(line_text, current_project_path, project_id):
    """Check if output mentions other repos."""
    if not line_text or not current_project_path:
        return False

    current_project_name = os.path.basename(current_project_path.rstrip('/'))

    # Check for mentions of other known repos
    for repo_path in safeguards.get("known_repos", []):
        repo_name = os.path.basename(repo_path)
        if repo_name == current_project_name:
            continue  # Skip current project

        # Check for repo name mentions in suspicious contexts
        suspicious_patterns = [
            f"cd {repo_path}",
            f"cd ~/{repo_name}",
            f"cd /Users/{repo_name}",
            f"{repo_path}/",
            f"/{repo_name}/TODO",
            f"/{repo_name}/src",
            f"checkout {repo_name}",
            f"project: {repo_name}",
            f"in {repo_name}",
        ]

        for pattern in suspicious_patterns:
            if pattern.lower() in line_text.lower():
                add_safeguard_alert(
                    "cross_repo",
                    f"Possible cross-repo activity detected: mentions '{repo_name}' while working on '{current_project_name}'",
                    project_id,
                    "warning"
                )
                return True

    return False

def get_safeguard_status():
    """Get current safeguard status for UI."""
    return {
        "alerts": safeguards["alerts"][-20:],  # Last 20 alerts
        "path_violations_count": len(safeguards["path_violations"]),
        "recent_violations": safeguards["path_violations"][-5:],
        "subagent_timeout_minutes": safeguards["subagent_timeout_minutes"]
    }

# Initialize known repos on module load
init_known_repos()

def get_serializable_state(state=None):
    """Return state dict without non-serializable objects (like Popen, set)."""
    if state is None:
        state = orchestra_state
    return {k: v for k, v in state.items() if k not in ("process", "files_changed_set")}

def get_all_projects_summary():
    """Get summary of all running projects for the UI."""
    summary = []
    for project_id, state in projects_state.items():
        summary.append({
            'id': project_id,
            'path': state.get('project_path', ''),
            'running': state.get('running', False),
            'current_cycle': state.get('current_cycle', 0),
            'cycles_completed': state.get('cycles_completed', 0),
            'current_stage': state.get('current_stage'),
            'prs_count': len(state.get('prs_created', [])),
            'files_changed': state.get('files_changed', 0),
            'subagent_count': state.get('subagent_count', 0)
        })
    return summary

def get_project_id_from_path(path):
    """Generate a short project ID from path."""
    return os.path.basename(path.rstrip('/')) or 'project'

def reset_daily_usage_if_needed():
    """Reset daily usage counter if it's a new day."""
    global usage_stats
    today = datetime.now().strftime('%Y-%m-%d')
    if usage_stats["last_reset_daily"] != today:
        usage_stats["requests_today"] = 0
        usage_stats["last_reset_daily"] = today

def reset_weekly_usage_if_needed():
    """Reset weekly usage counter if it's a new week."""
    global usage_stats
    # Week starts on Monday
    today = datetime.now()
    week_key = today.strftime('%Y-W%W')
    if usage_stats["last_reset_weekly"] != week_key:
        usage_stats["requests_this_week"] = 0
        usage_stats["last_reset_weekly"] = week_key

def track_api_request(tokens_estimate=1000):
    """Track an API request."""
    global usage_stats
    reset_daily_usage_if_needed()
    reset_weekly_usage_if_needed()
    usage_stats["requests_today"] += 1
    usage_stats["requests_this_week"] += 1
    usage_stats["tokens_estimated"] += tokens_estimate
    # Add to history
    usage_stats["history"].append({
        "timestamp": datetime.now().isoformat(),
        "requests": 1,
        "tokens": tokens_estimate
    })
    # Keep last 1000 entries
    if len(usage_stats["history"]) > 1000:
        usage_stats["history"] = usage_stats["history"][-1000:]
    socketio.emit('usage_update', get_usage_stats())

def get_usage_stats():
    """Get current usage stats for UI."""
    reset_daily_usage_if_needed()
    reset_weekly_usage_if_needed()
    return {
        "requests_today": usage_stats["requests_today"],
        "requests_this_week": usage_stats["requests_this_week"],
        "tokens_estimated": usage_stats["tokens_estimated"],
        "rate_limited": usage_stats["rate_limited"],
        "rate_limit_until": usage_stats["rate_limit_until"]
    }

def check_rate_limit(line):
    """Check if output indicates rate limiting. Returns seconds to wait or None."""
    global usage_stats
    # Common rate limit patterns
    rate_limit_patterns = [
        r"rate limit|rate-limit|ratelimit",
        r"too many requests",
        r"429",
        r"try again in (\d+)",
        r"wait (\d+) (second|minute|hour)",
        r"exceeded.*limit"
    ]
    line_lower = line.lower()
    for pattern in rate_limit_patterns:
        if re.search(pattern, line_lower):
            # Try to extract wait time
            time_match = re.search(r"(\d+)\s*(second|minute|hour|sec|min|hr)", line_lower)
            if time_match:
                amount = int(time_match.group(1))
                unit = time_match.group(2)
                if 'min' in unit:
                    amount *= 60
                elif 'hour' in unit or 'hr' in unit:
                    amount *= 3600
                usage_stats["rate_limited"] = True
                usage_stats["rate_limit_until"] = (datetime.now() + timedelta(seconds=amount)).isoformat()
                return amount
            # Default 60 second wait if no time found
            usage_stats["rate_limited"] = True
            usage_stats["rate_limit_until"] = (datetime.now() + timedelta(seconds=60)).isoformat()
            return 60
    return None

def clear_rate_limit():
    """Clear rate limit status."""
    global usage_stats
    usage_stats["rate_limited"] = False
    usage_stats["rate_limit_until"] = None

def add_summary_event(event_type, description, project_id=None):
    """Add an event to the summary data."""
    global summary_data
    now = datetime.now()
    hour_key = now.strftime('%Y-%m-%d-%H')
    day_key = now.strftime('%Y-%m-%d')

    # Add to events list
    summary_data["events"].append({
        "timestamp": now.isoformat(),
        "type": event_type,
        "description": description,
        "project_id": project_id
    })
    # Keep last 500 events
    if len(summary_data["events"]) > 500:
        summary_data["events"] = summary_data["events"][-500:]

    # Update hourly/daily aggregates
    if hour_key not in summary_data["hourly"]:
        summary_data["hourly"][hour_key] = {"prs": 0, "tasks": 0, "files": 0, "requests": 0}
    if day_key not in summary_data["daily"]:
        summary_data["daily"][day_key] = {"prs": 0, "tasks": 0, "files": 0, "requests": 0}

    if event_type == "pr_created":
        summary_data["hourly"][hour_key]["prs"] += 1
        summary_data["daily"][day_key]["prs"] += 1
    elif event_type == "task_completed":
        summary_data["hourly"][hour_key]["tasks"] += 1
        summary_data["daily"][day_key]["tasks"] += 1
    elif event_type == "file_changed":
        summary_data["hourly"][hour_key]["files"] += 1
        summary_data["daily"][day_key]["files"] += 1

    summary_data["hourly"][hour_key]["requests"] += 1
    summary_data["daily"][day_key]["requests"] += 1

def get_summary_stats(time_range='today'):
    """Get summary stats for the UI filtered by time range."""
    now = datetime.now()

    # Filter events based on time_range
    if time_range == 'hour':
        cutoff = now - timedelta(hours=1)
    elif time_range == 'today':
        cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif time_range == 'week':
        cutoff = now - timedelta(days=7)
    else:
        cutoff = now - timedelta(days=30)  # Default to month

    # Filter events by timestamp
    filtered_events = []
    for event in summary_data["events"]:
        try:
            event_time = datetime.fromisoformat(event.get("timestamp", ""))
            if event_time >= cutoff:
                filtered_events.append(event)
        except (ValueError, TypeError):
            # Include events without valid timestamps
            filtered_events.append(event)

    return {
        "hourly": summary_data["hourly"],
        "daily": summary_data["daily"],
        "recent_events": filtered_events[-50:],  # Last 50 filtered events
        "time_range": time_range
    }

def add_to_queue(message, project_id=None):
    """Add a message to the queue."""
    global message_queue, queue_counter
    queue_counter += 1
    item = {
        "id": queue_counter,
        "message": message,
        "project_id": project_id,
        "status": "pending",
        "created_at": datetime.now().isoformat()
    }
    message_queue.append(item)
    socketio.emit('queue_update', get_queue_status())
    return item

def get_queue_status():
    """Get queue status for UI."""
    return {
        "items": message_queue,
        "pending_count": len([m for m in message_queue if m["status"] == "pending"]),
        "processing_count": len([m for m in message_queue if m["status"] == "processing"])
    }

def process_next_queue_item(project_id):
    """Get next pending queue item for a project."""
    for item in message_queue:
        if item["status"] == "pending" and (item["project_id"] is None or item["project_id"] == project_id):
            item["status"] = "processing"
            socketio.emit('queue_update', get_queue_status())
            return item
    return None

def complete_queue_item(item_id, success=True):
    """Mark a queue item as complete."""
    for item in message_queue:
        if item["id"] == item_id:
            item["status"] = "completed" if success else "failed"
            item["completed_at"] = datetime.now().isoformat()
            socketio.emit('queue_update', get_queue_status())
            return

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Claude Orchestra Dashboard</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #0d1117;
            color: #c9d1d9;
            min-height: 100vh;
        }
        .header {
            background: linear-gradient(135deg, #238636 0%, #1f6feb 100%);
            padding: 20px 40px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .header h1 { font-size: 24px; color: white; }
        .status-badge {
            padding: 8px 16px;
            border-radius: 20px;
            font-weight: 600;
            font-size: 14px;
        }
        .status-running { background: #238636; color: white; }
        .status-stopped { background: #6e7681; color: white; }
        .container { padding: 20px 40px; }
        .grid {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
            margin-bottom: 20px;
        }
        .card {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            padding: 20px;
        }
        .card-title {
            font-size: 12px;
            color: #8b949e;
            text-transform: uppercase;
            margin-bottom: 8px;
        }
        .card-value {
            font-size: 32px;
            font-weight: 700;
            color: #58a6ff;
        }
        .card-value.success { color: #238636; }
        .card-value.warning { color: #d29922; }
        .main-grid {
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 20px;
        }
        .log-container {
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 12px;
            height: 600px;
            min-height: 400px;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }
        .log-header {
            padding: 15px 20px;
            background: #161b22;
            border-bottom: 1px solid #30363d;
            font-weight: 600;
        }
        .log-content {
            flex: 1;
            overflow-y: auto;
            overflow-x: hidden;
            padding: 15px;
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 12px;
            line-height: 1.6;
            scroll-behavior: smooth;
        }
        .log-line {
            margin-bottom: 4px;
            word-wrap: break-word;
            overflow-wrap: break-word;
            white-space: pre-wrap;
            max-width: 100%;
        }
        .log-line.new-line {
            animation: highlight-new 1s ease-out;
        }
        @keyframes highlight-new {
            from { background: rgba(88, 166, 255, 0.2); }
            to { background: transparent; }
        }
        .log-line-tool { color: #d29922; }
        .log-line-stage { color: #238636; font-weight: 600; }
        .log-line-error { color: #f85149; }
        .log-line-normal { color: #c9d1d9; }
        .sidebar .card { margin-bottom: 20px; }
        .pr-list { list-style: none; }
        .pr-item {
            padding: 12px;
            border-bottom: 1px solid #30363d;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .pr-item:last-child { border-bottom: none; }
        .pr-number {
            background: #238636;
            color: white;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
            font-weight: 600;
        }
        .pr-title {
            flex: 1;
            font-size: 13px;
            color: #c9d1d9;
        }
        .pr-link {
            color: #58a6ff;
            text-decoration: none;
            font-size: 12px;
        }
        .pr-link:hover { text-decoration: underline; }
        .stage-pipeline {
            display: flex;
            gap: 10px;
            margin-top: 15px;
        }
        .stage-item {
            flex: 1;
            padding: 12px;
            background: #21262d;
            border-radius: 8px;
            text-align: center;
            font-size: 12px;
            border: 2px solid transparent;
            position: relative;
        }
        .stage-item.active {
            border-color: #58a6ff;
            background: #1f6feb20;
            animation: pulse 2s infinite;
        }
        .stage-item.completed {
            border-color: #238636;
            background: #23863620;
        }
        .stage-item.idle {
            opacity: 0.6;
        }
        @keyframes pulse {
            0%, 100% { box-shadow: 0 0 0 0 rgba(88, 166, 255, 0.4); }
            50% { box-shadow: 0 0 0 8px rgba(88, 166, 255, 0); }
        }
        .stage-icon { font-size: 20px; margin-bottom: 5px; }
        .stage-status {
            font-size: 10px;
            color: #8b949e;
            margin-top: 4px;
            text-transform: uppercase;
        }
        .stage-status.running { color: #58a6ff; font-weight: 600; }
        .stage-status.done { color: #238636; }
        .controls {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
        }
        input, select, button {
            padding: 10px 16px;
            border-radius: 8px;
            border: 1px solid #30363d;
            background: #21262d;
            color: #c9d1d9;
            font-size: 14px;
        }
        input { flex: 1; }
        button {
            cursor: pointer;
            font-weight: 600;
            transition: all 0.2s;
        }
        button.primary {
            background: #238636;
            border-color: #238636;
            color: white;
        }
        button.primary:hover { background: #2ea043; }
        button.danger {
            background: #da3633;
            border-color: #da3633;
            color: white;
        }
        button.danger:hover { background: #f85149; }
        button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .time-remaining {
            font-size: 14px;
            color: #8b949e;
            margin-top: 5px;
        }
        .guidance-section {
            margin-bottom: 20px;
        }
        .guidance-section textarea {
            width: 100%;
            padding: 12px;
            border-radius: 8px;
            border: 1px solid #30363d;
            background: #21262d;
            color: #c9d1d9;
            font-size: 14px;
            font-family: inherit;
            resize: vertical;
            min-height: 60px;
        }
        .task-queue-section {
            margin-bottom: 20px;
        }
        .task-queue-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .task-queue-header h3 {
            font-size: 14px;
            color: #8b949e;
            text-transform: uppercase;
        }
        .task-list {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            max-height: 300px;
            overflow-y: auto;
        }
        .task-item {
            display: flex;
            align-items: flex-start;
            padding: 12px;
            border-bottom: 1px solid #30363d;
            gap: 10px;
        }
        .task-item:last-child { border-bottom: none; }
        .task-item input[type="checkbox"] {
            margin-top: 3px;
            width: 18px;
            height: 18px;
            cursor: pointer;
        }
        .task-item label {
            flex: 1;
            cursor: pointer;
            font-size: 13px;
            line-height: 1.4;
        }
        .task-item.priority-high label { color: #f85149; }
        .task-item.priority-medium label { color: #d29922; }
        .task-item.priority-low label { color: #8b949e; }
        .task-item.selected {
            background: #1f6feb20;
            border-left: 3px solid #58a6ff;
        }
        .queue-count {
            background: #238636;
            color: white;
            padding: 2px 8px;
            border-radius: 12px;
            font-size: 12px;
        }
        .btn-small {
            padding: 6px 12px;
            font-size: 12px;
        }
        .toggle-label {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 8px 12px;
            background: #21262d;
            border: 1px solid #30363d;
            border-radius: 8px;
            cursor: pointer;
            font-size: 13px;
            color: #8b949e;
            transition: all 0.2s;
        }
        .toggle-label:hover {
            border-color: #58a6ff;
            color: #c9d1d9;
        }
        .toggle-label input[type="checkbox"] {
            width: 16px;
            height: 16px;
            cursor: pointer;
        }
        .toggle-label input[type="checkbox"]:checked + span {
            color: #58a6ff;
            font-weight: 600;
        }
        .browse-btn {
            padding: 10px 14px;
            border-radius: 8px;
            border: 1px solid #30363d;
            background: #21262d;
            color: #c9d1d9;
            font-size: 16px;
            cursor: pointer;
            transition: all 0.2s;
        }
        .browse-btn:hover {
            background: #30363d;
            border-color: #58a6ff;
        }
        #recentProjects {
            max-width: 120px;
        }
        /* Directory Browser Modal */
        .modal-overlay {
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            width: 100%;
            height: 100%;
            background: rgba(0, 0, 0, 0.7);
            z-index: 1000;
            justify-content: center;
            align-items: center;
        }
        .modal-overlay.active {
            display: flex;
        }
        .modal {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 12px;
            width: 500px;
            max-height: 80vh;
            display: flex;
            flex-direction: column;
        }
        .modal-header {
            padding: 16px 20px;
            border-bottom: 1px solid #30363d;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .modal-header h2 {
            font-size: 16px;
            color: #c9d1d9;
        }
        .modal-close {
            background: none;
            border: none;
            color: #8b949e;
            font-size: 20px;
            cursor: pointer;
        }
        .modal-close:hover {
            color: #c9d1d9;
        }
        .modal-path {
            padding: 12px 20px;
            background: #0d1117;
            border-bottom: 1px solid #30363d;
            font-family: monospace;
            font-size: 13px;
            color: #58a6ff;
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .modal-path input {
            flex: 1;
            background: transparent;
            border: none;
            color: #58a6ff;
            font-family: monospace;
            font-size: 13px;
        }
        .modal-path input:focus {
            outline: none;
        }
        .modal-content {
            flex: 1;
            overflow-y: auto;
            max-height: 400px;
        }
        .dir-item {
            padding: 10px 20px;
            display: flex;
            align-items: center;
            gap: 10px;
            cursor: pointer;
            border-bottom: 1px solid #21262d;
        }
        .dir-item:hover {
            background: #21262d;
        }
        .dir-item.selected {
            background: #1f6feb30;
        }
        .dir-icon {
            font-size: 16px;
        }
        .dir-name {
            flex: 1;
            font-size: 14px;
            color: #c9d1d9;
        }
        .modal-footer {
            padding: 16px 20px;
            border-top: 1px solid #30363d;
            display: flex;
            justify-content: flex-end;
            gap: 10px;
        }
        /* Project Tabs */
        .project-tabs {
            display: flex;
            gap: 8px;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 1px solid #30363d;
            overflow-x: auto;
            flex-wrap: nowrap;
        }
        .project-tab {
            display: flex;
            align-items: center;
            gap: 8px;
            padding: 10px 16px;
            background: #21262d;
            border: 1px solid #30363d;
            border-radius: 8px;
            cursor: pointer;
            min-width: fit-content;
            transition: all 0.2s;
        }
        .project-tab:hover {
            border-color: #58a6ff;
            background: #30363d;
        }
        .project-tab.active {
            border-color: #58a6ff;
            background: #1f6feb20;
        }
        .project-tab.running {
            border-color: #238636;
            background: #23863620;
        }
        .project-tab.running .tab-status {
            color: #238636;
        }
        .project-tab.pending {
            border-color: #d29922;
            background: #d2992220;
        }
        .project-tab.pending .tab-status {
            color: #d29922;
        }
        .project-tab.add-btn {
            border-style: dashed;
            background: transparent;
        }
        .project-tab.add-btn:hover {
            border-color: #58a6ff;
            background: #21262d;
        }
        .tab-icon {
            font-size: 16px;
        }
        .tab-name {
            font-size: 13px;
            font-weight: 600;
            color: #c9d1d9;
            max-width: 120px;
            overflow: hidden;
            text-overflow: ellipsis;
            white-space: nowrap;
        }
        .tab-status {
            font-size: 10px;
            color: #8b949e;
            text-transform: uppercase;
        }
        .tab-close {
            font-size: 14px;
            color: #8b949e;
            cursor: pointer;
            margin-left: 4px;
        }
        .tab-close:hover {
            color: #f85149;
        }
        .running-projects-bar {
            display: flex;
            gap: 10px;
            padding: 10px 15px;
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            margin-bottom: 15px;
            flex-wrap: wrap;
        }
        .running-project-chip {
            display: flex;
            align-items: center;
            gap: 6px;
            padding: 6px 12px;
            background: #23863620;
            border: 1px solid #238636;
            border-radius: 20px;
            font-size: 12px;
        }
        .running-project-chip .status-dot {
            width: 8px;
            height: 8px;
            background: #238636;
            border-radius: 50%;
            animation: pulse-dot 2s infinite;
        }
        @keyframes pulse-dot {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.5; }
        }
        /* Usage Stats Bar */
        .usage-bar {
            display: flex;
            align-items: center;
            gap: 20px;
            padding: 12px 20px;
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            margin-bottom: 15px;
        }
        .usage-item {
            display: flex;
            align-items: center;
            gap: 8px;
        }
        .usage-label {
            font-size: 12px;
            color: #8b949e;
            text-transform: uppercase;
        }
        .usage-value {
            font-size: 16px;
            font-weight: 600;
            color: #58a6ff;
        }
        .usage-progress {
            flex: 1;
            height: 8px;
            background: #21262d;
            border-radius: 4px;
            overflow: hidden;
            min-width: 200px;
        }
        .usage-progress-fill {
            height: 100%;
            background: linear-gradient(90deg, #238636, #58a6ff);
            border-radius: 4px;
            transition: width 0.3s ease;
        }
        .usage-progress-fill.warning { background: linear-gradient(90deg, #d29922, #f85149); }
        .usage-progress-fill.danger { background: #f85149; }
        /* Rate Limit Warning */
        .rate-limit-warning {
            display: none;
            padding: 15px 20px;
            background: linear-gradient(135deg, #da363320, #d2992220);
            border: 1px solid #da3633;
            border-radius: 8px;
            margin-bottom: 15px;
            animation: pulse-warning 2s infinite;
        }
        .rate-limit-warning.active { display: flex; }
        .rate-limit-warning .warning-icon { font-size: 24px; margin-right: 15px; }
        .rate-limit-warning .warning-content { flex: 1; }
        .rate-limit-warning .warning-title {
            font-size: 16px;
            font-weight: 600;
            color: #f85149;
            margin-bottom: 4px;
        }
        .rate-limit-warning .warning-countdown {
            font-size: 24px;
            font-weight: 700;
            color: #d29922;
        }
        .rate-limit-warning .warning-text { font-size: 13px; color: #8b949e; }
        @keyframes pulse-warning {
            0%, 100% { border-color: #da3633; }
            50% { border-color: #d29922; }
        }
        /* Message Queue Form */
        .message-queue-section {
            margin-bottom: 20px;
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 15px;
        }
        .message-queue-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .message-queue-header h3 {
            font-size: 14px;
            color: #8b949e;
            text-transform: uppercase;
        }
        .message-input-area {
            display: flex;
            gap: 10px;
        }
        .message-input-area textarea {
            flex: 1;
            min-height: 60px;
            padding: 12px;
            border-radius: 8px;
            border: 1px solid #30363d;
            background: #21262d;
            color: #c9d1d9;
            font-size: 14px;
            font-family: inherit;
            resize: vertical;
        }
        .queue-list {
            margin-top: 15px;
            max-height: 200px;
            overflow-y: auto;
        }
        .queue-item {
            display: flex;
            align-items: center;
            padding: 10px;
            background: #21262d;
            border-radius: 6px;
            margin-bottom: 8px;
            gap: 10px;
        }
        .queue-item .queue-status {
            width: 8px;
            height: 8px;
            border-radius: 50%;
            background: #8b949e;
        }
        .queue-item .queue-status.pending { background: #d29922; }
        .queue-item .queue-status.processing { background: #238636; animation: pulse-dot 1s infinite; }
        .queue-item .queue-text { flex: 1; font-size: 13px; color: #c9d1d9; }
        .queue-item .queue-remove { color: #8b949e; cursor: pointer; }
        .queue-item .queue-remove:hover { color: #f85149; }
        /* Summary/Master View */
        .summary-section {
            margin-bottom: 20px;
        }
        .summary-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .summary-header h3 {
            font-size: 14px;
            color: #8b949e;
            text-transform: uppercase;
        }
        .summary-tabs {
            display: flex;
            gap: 5px;
        }
        .summary-tab {
            padding: 6px 12px;
            background: #21262d;
            border: 1px solid #30363d;
            border-radius: 6px;
            font-size: 12px;
            cursor: pointer;
            color: #8b949e;
        }
        .summary-tab.active {
            background: #1f6feb20;
            border-color: #58a6ff;
            color: #58a6ff;
        }
        .summary-content {
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 8px;
            padding: 15px;
            max-height: 300px;
            overflow-y: auto;
        }
        .summary-event {
            display: flex;
            gap: 10px;
            padding: 8px 0;
            border-bottom: 1px solid #21262d;
        }
        .summary-event:last-child { border-bottom: none; }
        .summary-event .event-time {
            font-size: 11px;
            color: #8b949e;
            min-width: 60px;
        }
        .summary-event .event-type {
            font-size: 11px;
            padding: 2px 6px;
            border-radius: 4px;
            background: #21262d;
            color: #58a6ff;
            min-width: 60px;
            text-align: center;
        }
        .summary-event .event-message { flex: 1; font-size: 13px; color: #c9d1d9; }
        .summary-stats {
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 10px;
            margin-bottom: 15px;
        }
        .summary-stat {
            background: #21262d;
            border-radius: 6px;
            padding: 10px;
            text-align: center;
        }
        .summary-stat .stat-value {
            font-size: 20px;
            font-weight: 700;
            color: #58a6ff;
        }
        .summary-stat .stat-label {
            font-size: 10px;
            color: #8b949e;
            text-transform: uppercase;
        }

        /* Multi-User Panel - Injected from dashboard_claims.py */
        .multiuser-panel { margin-top: 20px; }
        .multiuser-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px; cursor: pointer; }
        .multiuser-header h3 { font-size: 14px; color: #8b949e; text-transform: uppercase; display: flex; align-items: center; gap: 8px; }
        .multiuser-status { padding: 4px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; }
        .multiuser-status.configured { background: #238636; color: white; }
        .multiuser-status.not-configured { background: #6e7681; color: white; }
        .multiuser-content { display: none; }
        .multiuser-content.expanded { display: block; }
        .setup-form { background: #0d1117; border: 1px solid #30363d; border-radius: 8px; padding: 15px; margin-bottom: 15px; }
        .setup-form h4 { font-size: 13px; color: #c9d1d9; margin-bottom: 12px; }
        .form-group { margin-bottom: 12px; }
        .form-group label { display: block; font-size: 11px; color: #8b949e; margin-bottom: 4px; text-transform: uppercase; }
        .form-group input { width: 100%; padding: 8px 10px; background: #21262d; border: 1px solid #30363d; border-radius: 6px; color: #c9d1d9; font-size: 13px; }
        .form-group input:focus { outline: none; border-color: #58a6ff; }
        .form-group input.success { border-color: #238636; }
        .form-group input.error { border-color: #f85149; }
        .form-group small { display: block; margin-top: 4px; font-size: 10px; color: #6e7681; }
        .form-row { display: flex; gap: 10px; }
        .form-row .form-group { flex: 1; }
        .form-actions { display: flex; gap: 8px; margin-top: 15px; }
        .form-actions button { flex: 1; padding: 8px 12px; border-radius: 6px; font-size: 12px; font-weight: 600; cursor: pointer; border: 1px solid #30363d; background: #21262d; color: #c9d1d9; }
        .form-actions button:hover { background: #30363d; }
        .form-actions button.primary { background: #238636; border-color: #238636; color: white; }
        .form-actions button.primary:hover { background: #2ea043; }
        .form-actions button:disabled { opacity: 0.5; cursor: not-allowed; }
        .connection-status { padding: 10px; border-radius: 6px; margin-top: 10px; font-size: 12px; display: none; }
        .connection-status.success { display: block; background: #23863620; border: 1px solid #238636; color: #238636; }
        .connection-status.error { display: block; background: #f8514920; border: 1px solid #f85149; color: #f85149; }
        .sync-results { padding: 10px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; margin-top: 10px; font-size: 12px; }
        .sync-results .stat { display: flex; justify-content: space-between; padding: 4px 0; }
        .sync-results .stat-value { font-weight: 600; color: #58a6ff; }
        .claims-section { margin-top: 15px; }
        .claims-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px; }
        .claims-header h4 { font-size: 12px; color: #8b949e; }
        .claims-badge { background: #238636; color: white; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }
        .claims-list { list-style: none; max-height: 200px; overflow-y: auto; }
        .claim-item { padding: 10px; background: #21262d; border-radius: 6px; margin-bottom: 8px; border-left: 3px solid #30363d; }
        .claim-item.mine { border-left-color: #238636; }
        .claim-item.stale { border-left-color: #d29922; opacity: 0.8; }
        .claim-issue { font-weight: 600; color: #58a6ff; font-size: 13px; }
        .claim-agent { font-size: 10px; color: #8b949e; font-family: monospace; margin-top: 4px; }
        .claim-meta { display: flex; justify-content: space-between; align-items: center; margin-top: 6px; }
        .heartbeat-indicator { display: flex; align-items: center; gap: 5px; font-size: 10px; }
        .heartbeat-dot { width: 6px; height: 6px; border-radius: 50%; }
        .heartbeat-dot.fresh { background: #238636; }
        .heartbeat-dot.warning { background: #d29922; }
        .heartbeat-dot.stale { background: #f85149; }
        .claim-release-btn { padding: 3px 6px; font-size: 9px; background: transparent; border: 1px solid #f85149; color: #f85149; border-radius: 4px; cursor: pointer; }
        .claim-release-btn:hover { background: #f8514920; }
        .available-section { margin-top: 15px; }
        .available-section h4 { font-size: 12px; color: #8b949e; margin-bottom: 10px; }
        .task-item { padding: 8px 10px; background: #0d1117; border: 1px solid #30363d; border-radius: 6px; margin-bottom: 6px; display: flex; align-items: center; gap: 8px; }
        .task-number { background: #30363d; color: #8b949e; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600; }
        .task-title { flex: 1; font-size: 11px; color: #c9d1d9; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .task-labels { display: flex; gap: 4px; }
        .task-label { padding: 2px 5px; border-radius: 3px; font-size: 9px; font-weight: 500; }
        .task-label.high { background: #b6020530; color: #f85149; }
        .task-label.medium { background: #d2992230; color: #d29922; }
        .task-label.low { background: #23863630; color: #238636; }
        .refresh-actions { display: flex; gap: 8px; margin-top: 10px; }
        .refresh-actions button { flex: 1; padding: 6px 10px; font-size: 11px; background: #21262d; border: 1px solid #30363d; color: #8b949e; border-radius: 4px; cursor: pointer; }
        .refresh-actions button:hover { background: #30363d; color: #c9d1d9; }
        .spinner { display: inline-block; width: 12px; height: 12px; border: 2px solid #30363d; border-top-color: #58a6ff; border-radius: 50%; animation: spin 1s linear infinite; }
        @keyframes spin { to { transform: rotate(360deg); } }
        .hidden { display: none !important; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Claude Orchestra Dashboard</h1>
        <span class="status-badge" id="statusBadge">Stopped</span>
    </div>

    <div class="container">
        <!-- Project Tabs -->
        <div class="project-tabs" id="projectTabs">
            <div class="project-tab active" data-project="new" onclick="selectProject('new')">
                <span class="tab-icon">➕</span>
                <span class="tab-name">Add Project</span>
            </div>
        </div>

        <div class="controls">
            <input type="text" id="projectPath" placeholder="Project path (e.g., /Users/you/project)" value="" onchange="savePendingProjectConfig()">
            <button class="browse-btn" onclick="openBrowser()" title="Browse for folder">📁</button>
            <select id="recentProjects" onchange="selectRecentProject()" title="Recent projects">
                <option value="">Recent...</option>
            </select>
            <select id="maxHours" onchange="savePendingProjectConfig()">
                <option value="0">Indefinite</option>
                <option value="0.5">30 minutes</option>
                <option value="1" selected>1 hour</option>
                <option value="2">2 hours</option>
                <option value="4">4 hours</option>
                <option value="8">8 hours</option>
                <option value="24">24 hours</option>
            </select>
            <select id="taskMode" onchange="savePendingProjectConfig()">
                <option value="small">Small Tasks</option>
                <option value="normal" selected>Normal</option>
                <option value="large">Large Features</option>
            </select>
            <select id="modelSelect" onchange="savePendingProjectConfig()">
                <option value="haiku">Haiku (Fast)</option>
                <option value="sonnet" selected>Sonnet (Balanced)</option>
                <option value="opus">Opus (Most Capable)</option>
            </select>
            <label class="toggle-label" title="Use specialized sub-agents for code review, testing, debugging, and security">
                <input type="checkbox" id="useSubAgents" checked>
                <span>Sub-Agents</span>
            </label>
            <button class="primary" id="startBtn" onclick="startOrchestra()">Start Orchestra</button>
            <button class="danger" id="stopBtn" onclick="stopOrchestra()" disabled>Stop</button>
        </div>

        <div class="guidance-section">
            <textarea id="initialGuidance" placeholder="Optional: Initial guidance for the orchestra (e.g., 'Focus on backend API tasks first' or 'Start with the authentication module')" onchange="savePendingProjectConfig()"></textarea>
        </div>

        <!-- Usage Stats Bar -->
        <div class="usage-bar" id="usageBar">
            <div class="usage-item">
                <span class="usage-label">Today</span>
                <span class="usage-value" id="usageToday">0/20</span>
            </div>
            <div class="usage-item">
                <span class="usage-label">This Week</span>
                <span class="usage-value" id="usageWeek">0/100</span>
            </div>
            <div class="usage-progress">
                <div class="usage-progress-fill" id="usageProgressFill" style="width: 0%"></div>
            </div>
            <div class="usage-item">
                <span class="usage-label">Tokens Est.</span>
                <span class="usage-value" id="usageTokens">0</span>
            </div>
        </div>

        <!-- Rate Limit Warning -->
        <div class="rate-limit-warning" id="rateLimitWarning">
            <span class="warning-icon">⚠️</span>
            <div class="warning-content">
                <div class="warning-title">Rate Limit Reached</div>
                <div class="warning-text">Will auto-resume in: <span class="warning-countdown" id="rateLimitCountdown">--:--</span></div>
            </div>
            <button class="btn-small" onclick="clearRateLimit()">Clear & Resume</button>
        </div>

        <!-- Message Queue (for queuing messages to send) -->
        <div class="message-queue-section">
            <div class="message-queue-header">
                <h3>Message Queue <span class="queue-count" id="messageQueueCount">0</span></h3>
            </div>
            <div class="message-input-area">
                <textarea id="queueMessageInput" placeholder="Type a message to queue up for the next available slot..."></textarea>
                <button class="primary" onclick="addToMessageQueue()">Queue</button>
            </div>
            <div class="queue-list" id="messageQueueList"></div>
        </div>

        <!-- Summary/Master View -->
        <div class="summary-section">
            <div class="summary-header">
                <h3>Activity Summary</h3>
                <div class="summary-tabs">
                    <span class="summary-tab active" onclick="switchSummaryTab('events')">Events</span>
                    <span class="summary-tab" onclick="switchSummaryTab('hourly')">Hourly</span>
                    <span class="summary-tab" onclick="switchSummaryTab('daily')">Daily</span>
                </div>
            </div>
            <div class="summary-stats" id="summaryStats">
                <div class="summary-stat">
                    <div class="stat-value" id="summaryRequests">0</div>
                    <div class="stat-label">Requests</div>
                </div>
                <div class="summary-stat">
                    <div class="stat-value" id="summaryTokens">0</div>
                    <div class="stat-label">Tokens</div>
                </div>
                <div class="summary-stat">
                    <div class="stat-value" id="summaryProjects">0</div>
                    <div class="stat-label">Projects</div>
                </div>
                <div class="summary-stat">
                    <div class="stat-value" id="summaryAlerts">0</div>
                    <div class="stat-label">Alerts</div>
                </div>
            </div>
            <div class="summary-content" id="summaryContent">
                <div style="color: #8b949e; text-align: center; padding: 20px;">No events yet</div>
            </div>
        </div>

        <div class="task-queue-section">
            <div class="task-queue-header">
                <h3>Task Queue <span class="queue-count" id="queueCount">0 selected</span></h3>
                <div>
                    <button class="btn-small" onclick="loadTodos()">Load TODOs</button>
                    <button class="btn-small" onclick="selectAll()">Select All</button>
                    <button class="btn-small" onclick="clearSelection()">Clear</button>
                </div>
            </div>
            <div class="task-list" id="taskList">
                <div class="task-item" style="color: #8b949e; justify-content: center;">
                    Click "Load TODOs" to see available tasks
                </div>
            </div>
        </div>

        <div class="grid">
            <div class="card">
                <div class="card-title">Current Cycle</div>
                <div class="card-value" id="currentCycle">0</div>
            </div>
            <div class="card">
                <div class="card-title">Cycles Completed</div>
                <div class="card-value success" id="cyclesCompleted">0</div>
            </div>
            <div class="card">
                <div class="card-title">PRs Created</div>
                <div class="card-value" id="prsCreated">0</div>
            </div>
            <div class="card">
                <div class="card-title">Time Elapsed</div>
                <div class="card-value warning" id="timeElapsed">00:00</div>
                <div class="time-remaining" id="timeRemaining"></div>
            </div>
        </div>

        <div class="grid" style="grid-template-columns: repeat(4, 1fr);">
            <div class="card">
                <div class="card-title">Branches Created</div>
                <div class="card-value" id="branchesCreated">0</div>
                <div class="time-remaining" id="currentBranch">-</div>
            </div>
            <div class="card">
                <div class="card-title">Files Changed</div>
                <div class="card-value" id="filesChanged">0</div>
                <div class="time-remaining" id="lastFile">-</div>
            </div>
            <div class="card">
                <div class="card-title">Sub-Agents</div>
                <div class="card-value" id="subAgentCount">0</div>
                <div class="time-remaining" id="activeSubAgent">-</div>
            </div>
            <div class="card">
                <div class="card-title">Tools Used</div>
                <div class="card-value" id="toolsUsed">0</div>
                <div class="time-remaining" id="lastTool">-</div>
            </div>
        </div>

        <div class="card" style="margin-bottom: 20px;">
            <div class="card-title">Agent Pipeline</div>
            <div class="stage-pipeline">
                <div class="stage-item idle" id="stage-implement">
                    <div class="stage-icon">🔨</div>
                    Implementer
                    <div class="stage-status" id="status-implement">Idle</div>
                </div>
                <div class="stage-item idle" id="stage-test">
                    <div class="stage-icon">🧪</div>
                    Tester
                    <div class="stage-status" id="status-test">Idle</div>
                </div>
                <div class="stage-item idle" id="stage-review">
                    <div class="stage-icon">👀</div>
                    Reviewer
                    <div class="stage-status" id="status-review">Idle</div>
                </div>
                <div class="stage-item idle" id="stage-plan">
                    <div class="stage-icon">📋</div>
                    Planner
                    <div class="stage-status" id="status-plan">Idle</div>
                </div>
            </div>
        </div>

        <div class="main-grid">
            <div class="log-container">
                <div class="log-header">Live Output</div>
                <div class="log-content" id="logContent"></div>
            </div>

            <div class="sidebar">
                <div class="card">
                    <div class="card-title">Pull Requests</div>
                    <ul class="pr-list" id="prList">
                        <li class="pr-item" style="color: #8b949e;">No PRs yet</li>
                    </ul>
                </div>
                <div class="card">
                    <div class="card-title">Activity Log</div>
                    <ul class="pr-list" id="activityLog">
                        <li class="pr-item" style="color: #8b949e;">No activity yet</li>
                    </ul>
                </div>
                <div class="card">
                    <div class="card-title">Sub-Agents Used</div>
                    <ul class="pr-list" id="subagentsList">
                        <li class="pr-item" style="color: #8b949e;">None yet</li>
                    </ul>
                </div>

                <!-- Multi-User Mode Panel -->
                <div class="card multiuser-panel">
                    <div class="multiuser-header" onclick="toggleMultiUserPanel()">
                        <h3>
                            👥 Multi-User Mode
                            <span class="multiuser-status not-configured" id="multiuser-status">Not Configured</span>
                        </h3>
                        <span id="multiuser-toggle">▼</span>
                    </div>

                    <div class="multiuser-content" id="multiuser-content">
                        <!-- Setup Form -->
                        <div class="setup-form" id="setup-form">
                            <h4>⚙️ Configuration</h4>

                            <div class="form-group">
                                <label>GitHub Token</label>
                                <input type="password" id="github-token" placeholder="ghp_xxxxxxxxxxxx">
                                <small>Get from: github.com/settings/tokens (needs 'repo' scope)</small>
                            </div>

                            <div class="form-group">
                                <label>Repository</label>
                                <input type="text" id="github-repo" placeholder="owner/repo">
                            </div>

                            <div class="form-row">
                                <div class="form-group">
                                    <label>Claim Timeout (sec)</label>
                                    <input type="number" id="claim-timeout" value="1800">
                                </div>
                                <div class="form-group">
                                    <label>Heartbeat (sec)</label>
                                    <input type="number" id="heartbeat-interval" value="300">
                                </div>
                            </div>

                            <div class="form-actions">
                                <button onclick="testConnection()" id="test-btn">
                                    🔌 Test Connection
                                </button>
                                <button onclick="saveConfig()" class="primary" id="save-btn">
                                    💾 Save & Enable
                                </button>
                            </div>

                            <div class="connection-status" id="connection-status"></div>
                        </div>

                        <!-- Sync Section -->
                        <div class="setup-form" id="sync-section">
                            <h4>📋 Sync TODO.md → GitHub Issues</h4>
                            <p style="font-size: 11px; color: #8b949e; margin-bottom: 10px;">
                                Creates GitHub Issues from your TODO.md file for task coordination.
                            </p>
                            <button onclick="syncTodos()" id="sync-btn" style="width: 100%;">
                                🔄 Sync Now
                            </button>
                            <div class="sync-results hidden" id="sync-results">
                                <div class="stat"><span>Created:</span><span class="stat-value" id="sync-created">0</span></div>
                                <div class="stat"><span>Updated:</span><span class="stat-value" id="sync-updated">0</span></div>
                                <div class="stat"><span>Unchanged:</span><span class="stat-value" id="sync-unchanged">0</span></div>
                            </div>
                        </div>

                        <!-- Active Claims -->
                        <div class="claims-section">
                            <div class="claims-header">
                                <h4>🔒 Active Claims</h4>
                                <span class="claims-badge" id="claims-count">0</span>
                            </div>
                            <ul class="claims-list" id="claims-list">
                                <li style="color: #6e7681; font-size: 11px;">No active claims</li>
                            </ul>
                        </div>

                        <!-- Available Tasks -->
                        <div class="available-section">
                            <h4>📝 Available Tasks (<span id="available-count">0</span>)</h4>
                            <div id="available-tasks-list">
                                <div style="color: #6e7681; font-size: 11px;">Configure multi-user mode to see tasks</div>
                            </div>
                        </div>

                        <!-- Refresh Actions -->
                        <div class="refresh-actions">
                            <button onclick="refreshClaims()">↻ Refresh</button>
                            <button onclick="reclaimStale()">🧹 Release Stale</button>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <script>
        const socket = io();
        let startTime = null;
        let maxSeconds = null;
        let timerInterval = null;

        // Multi-project state
        let currentProjectId = 'new';
        let projectsData = {};

        // Pending projects (configured but not started yet)
        let pendingProjects = {};  // { pending_1: { path: '...', prompt: '...' }, ... }
        let pendingCounter = 0;

        socket.on('connect', function() {
            console.log('Connected to server');
            socket.emit('get_state');
            socket.emit('get_all_projects');
            loadRecentProjects();
            // Create initial pending project if none exist
            if (Object.keys(pendingProjects).length === 0 && Object.keys(projectsData).length === 0) {
                addNewPendingProject();
            }
        });

        socket.on('projects_update', function(data) {
            projectsData = {};
            (data.projects || []).forEach(function(p) {
                projectsData[p.id] = p;
            });
            // If we have running projects but current is a pending, stay on pending
            // If current is 'new', switch to first running project or create pending
            if (currentProjectId === 'new') {
                if (Object.keys(projectsData).length > 0) {
                    currentProjectId = Object.keys(projectsData)[0];
                } else if (Object.keys(pendingProjects).length === 0) {
                    addNewPendingProject();
                    return;  // addNewPendingProject calls updateProjectTabs
                }
            }
            updateProjectTabs();
        });

        function updateProjectTabs() {
            console.log('updateProjectTabs called, projectsData:', projectsData, 'pendingProjects:', pendingProjects);
            var tabs = document.getElementById('projectTabs');
            if (!tabs) {
                console.error('projectTabs element not found!');
                return;
            }
            tabs.innerHTML = '';

            // Add tabs for running/stopped projects
            Object.keys(projectsData).forEach(function(id) {
                var project = projectsData[id];
                var tab = document.createElement('div');
                tab.className = 'project-tab' + (id === currentProjectId ? ' active' : '') + (project.running ? ' running' : '');
                tab.setAttribute('data-project', id);
                tab.onclick = function() { selectProject(id); };

                var icon = project.running ? '🟢' : '📁';
                var status = project.running ? 'Cycle ' + project.current_cycle : 'Stopped';

                tab.innerHTML = '<span class="tab-icon">' + icon + '</span>' +
                               '<span class="tab-name">' + id + '</span>' +
                               '<span class="tab-status">' + status + '</span>' +
                               '<span class="tab-close" onclick="event.stopPropagation(); removeProject(\\'' + id + '\\')">&times;</span>';
                tabs.appendChild(tab);
            });

            // Add tabs for pending (not yet started) projects
            Object.keys(pendingProjects).forEach(function(id) {
                var pending = pendingProjects[id];
                var tab = document.createElement('div');
                tab.className = 'project-tab pending' + (id === currentProjectId ? ' active' : '');
                tab.setAttribute('data-project', id);
                tab.onclick = function() { selectProject(id); };

                // Show folder name if path set, otherwise "New Project"
                var name = pending.path ? pending.path.split('/').pop() || 'New Project' : 'New Project';
                var icon = '⚙️';
                var status = 'Setup';

                tab.innerHTML = '<span class="tab-icon">' + icon + '</span>' +
                               '<span class="tab-name">' + name + '</span>' +
                               '<span class="tab-status">' + status + '</span>' +
                               '<span class="tab-close" onclick="event.stopPropagation(); removePendingProject(\\'' + id + '\\')">&times;</span>';
                tabs.appendChild(tab);
            });

            // Add "Add Project" button at the end
            var addTab = document.createElement('div');
            addTab.className = 'project-tab add-btn';
            addTab.setAttribute('data-project', 'add');
            addTab.onclick = function() { console.log('Add tab clicked'); addNewPendingProject(); };
            addTab.innerHTML = '<span class="tab-icon">➕</span><span class="tab-name">Add Project</span>';
            tabs.appendChild(addTab);
            console.log('Tabs updated, children count:', tabs.children.length);
        }

        function addNewPendingProject() {
            pendingCounter++;
            var newId = 'pending_' + pendingCounter;
            pendingProjects[newId] = { path: '', prompt: '', maxHours: '', maxCycles: '', model: 'sonnet' };
            currentProjectId = newId;
            updateProjectTabs();
            // Show setup view for this pending project
            showPendingProjectSetup(newId);
        }

        function removePendingProject(pendingId) {
            delete pendingProjects[pendingId];
            if (currentProjectId === pendingId) {
                // Switch to another tab
                var keys = Object.keys(projectsData).concat(Object.keys(pendingProjects));
                if (keys.length > 0) {
                    selectProject(keys[0]);
                } else {
                    addNewPendingProject();
                }
            } else {
                updateProjectTabs();
            }
        }

        function showPendingProjectSetup(pendingId) {
            var pending = pendingProjects[pendingId];
            if (!pending) return;

            // Populate form with pending project data
            document.getElementById('projectPath').value = pending.path || '';
            document.getElementById('initialGuidance').value = pending.guidance || '';
            document.getElementById('maxHours').value = pending.maxHours || '1';
            document.getElementById('taskMode').value = pending.taskMode || 'normal';
            if (document.getElementById('modelSelect')) {
                document.getElementById('modelSelect').value = pending.model || 'sonnet';
            }

            document.getElementById('startBtn').disabled = false;
            document.getElementById('stopBtn').disabled = true;
            document.getElementById('statusBadge').textContent = 'Setup';
            document.getElementById('statusBadge').className = 'status-badge status-stopped';

            // Clear the activity panels
            document.getElementById('logContent').innerHTML = '<div class="log-line log-line-stage">Select a project folder and click "Start Orchestra" to begin</div>';
            document.getElementById('activityLog').innerHTML = '<li class="pr-item" style="color: #8b949e;">No activity yet</li>';
            document.getElementById('subagentsList').innerHTML = '<li class="pr-item" style="color: #8b949e;">None yet</li>';
            document.getElementById('prList').innerHTML = '<li class="pr-item" style="color: #8b949e;">No PRs yet</li>';

            // Reset stats
            document.getElementById('currentCycle').textContent = '0';
            document.getElementById('cyclesCompleted').textContent = '0';
            document.getElementById('prsCreated').textContent = '0';
            document.getElementById('timeElapsed').textContent = '00:00';
            document.getElementById('branchesCreated').textContent = '0';
            document.getElementById('filesChanged').textContent = '0';
            document.getElementById('subAgentCount').textContent = '0';
            document.getElementById('toolsUsed').textContent = '0';

            // Open browser for new empty projects
            if (!pending.path) {
                openBrowser();
            }
        }

        // Save pending project config when form fields change
        function savePendingProjectConfig() {
            if (!currentProjectId || !currentProjectId.startsWith('pending_')) return;
            if (!pendingProjects[currentProjectId]) return;

            pendingProjects[currentProjectId].path = document.getElementById('projectPath').value;
            pendingProjects[currentProjectId].guidance = document.getElementById('initialGuidance').value;
            pendingProjects[currentProjectId].maxHours = document.getElementById('maxHours').value;
            pendingProjects[currentProjectId].taskMode = document.getElementById('taskMode').value;
            var modelSelect = document.getElementById('modelSelect');
            if (modelSelect) {
                pendingProjects[currentProjectId].model = modelSelect.value;
            }
            // Update tab name if path changed
            updateProjectTabs();
        }

        function selectProject(projectId) {
            console.log('selectProject called with:', projectId);
            currentProjectId = projectId;
            updateProjectTabs();

            if (projectId === 'new') {
                // Legacy - redirect to creating a new pending project
                addNewPendingProject();
            } else if (projectId.startsWith('pending_')) {
                // Show setup for pending project
                showPendingProjectSetup(projectId);
            } else {
                // Load existing project state from server
                socket.emit('get_project_state', { project_id: projectId });
            }
        }

        function removeProject(projectId) {
            if (projectsData[projectId] && projectsData[projectId].running) {
                if (!confirm('This project is running. Stop and remove it?')) {
                    return;
                }
                socket.emit('stop_project', { project_id: projectId });
            }
            socket.emit('remove_project', { project_id: projectId });
            if (currentProjectId === projectId) {
                // Switch to another available tab
                var keys = Object.keys(projectsData).filter(k => k !== projectId).concat(Object.keys(pendingProjects));
                if (keys.length > 0) {
                    selectProject(keys[0]);
                } else {
                    addNewPendingProject();
                }
            }
        }

        socket.on('project_state', function(data) {
            if (data.project_id === currentProjectId) {
                updateUI(data.state);
            }
        });

        function loadRecentProjects() {
            fetch('/api/recent-projects')
                .then(response => response.json())
                .then(data => {
                    var select = document.getElementById('recentProjects');
                    select.innerHTML = '<option value="">Recent...</option>';
                    (data.projects || []).forEach(function(path) {
                        var opt = document.createElement('option');
                        opt.value = path;
                        // Show just the last folder name for brevity
                        var parts = path.split('/');
                        opt.textContent = parts[parts.length - 1] || path;
                        select.appendChild(opt);
                    });
                })
                .catch(err => console.log('Could not load recent projects:', err));
        }

        function selectRecentProject() {
            var select = document.getElementById('recentProjects');
            if (select.value) {
                document.getElementById('projectPath').value = select.value;
                // Auto-load TODOs for the selected project
                loadTodos();
            }
            select.value = '';  // Reset dropdown
        }

        socket.on('state_update', function(state) {
            // Only update UI if this is for the current project or no project selected
            if (!state.project_id || state.project_id === currentProjectId || currentProjectId === 'new') {
                // For 'new' project, only update if state is for the initially loaded global state
                if (currentProjectId === 'new' && state.project_id && state.running) {
                    return; // Don't update "new project" view with running project's updates
                }
                updateUI(state);
            }
        });

        socket.on('log_line', function(data) {
            // Only add log line if it's for the current project or no project specified
            if (!data.project_id || data.project_id === currentProjectId) {
                addLogLine(data.line);
            }
        });

        socket.on('activity_update', function(data) {
            // Only update if this is for the current project
            if (!data.project_id || data.project_id === currentProjectId) {
                updateActivityStats(data);
            }
        });

        function updateActivityStats(activity) {
            // Update branch stats
            document.getElementById('branchesCreated').textContent = activity.branches_created || 0;
            if (activity.current_branch) {
                document.getElementById('currentBranch').textContent = activity.current_branch;
            }

            // Update files stats
            document.getElementById('filesChanged').textContent = activity.files_changed || 0;
            if (activity.last_file) {
                // Show just the filename, not full path
                var lastFile = activity.last_file.split('/').pop();
                document.getElementById('lastFile').textContent = lastFile;
            }

            // Update sub-agent stats
            document.getElementById('subAgentCount').textContent = activity.subagent_count || 0;
            if (activity.active_subagent) {
                document.getElementById('activeSubAgent').textContent = activity.active_subagent;
                document.getElementById('activeSubAgent').style.color = '#58a6ff';
            } else {
                document.getElementById('activeSubAgent').textContent = '-';
                document.getElementById('activeSubAgent').style.color = '#8b949e';
            }

            // Update tool stats
            document.getElementById('toolsUsed').textContent = activity.tools_used || 0;
            if (activity.last_tool) {
                document.getElementById('lastTool').textContent = activity.last_tool;
            }

            // Update sub-agents list
            if (activity.subagents_used && activity.subagents_used.length > 0) {
                var subagentsList = document.getElementById('subagentsList');
                subagentsList.innerHTML = '';
                activity.subagents_used.forEach(function(agent) {
                    var li = document.createElement('li');
                    li.className = 'pr-item';
                    var icon = getSubagentIcon(agent);
                    li.innerHTML = '<span style="margin-right: 8px;">' + icon + '</span>' +
                                   '<span class="pr-title">' + agent + '</span>';
                    if (agent === activity.active_subagent) {
                        li.style.background = '#1f6feb20';
                        li.innerHTML += '<span style="color: #58a6ff; font-size: 10px;">ACTIVE</span>';
                    }
                    subagentsList.appendChild(li);
                });
            }
        }

        function getSubagentIcon(agentType) {
            var icons = {
                'code-reviewer': '👀',
                'test-automator': '🧪',
                'debugger': '🔧',
                'security-auditor': '🔒',
                'Explore': '🔍',
                'Plan': '📋',
                'performance-engineer': '⚡',
                'docs-architect': '📚',
                'backend-architect': '🏗️',
                'deployment-engineer': '🚀'
            };
            return icons[agentType] || '🤖';
        }

        socket.on('activity_log_entry', function(entry) {
            // Only add if this is for the current project or no project filter
            if (!entry.project_id || entry.project_id === currentProjectId) {
                addActivityLogEntry(entry);
            }
        });

        function addActivityLogEntry(entry) {
            var activityLog = document.getElementById('activityLog');
            if (activityLog.children.length === 1 && activityLog.children[0].textContent.indexOf('No activity') !== -1) {
                activityLog.innerHTML = '';
            }

            var li = document.createElement('li');
            li.className = 'pr-item';

            var icon = '📝';
            var text = '';
            if (entry.type === 'branch') {
                icon = '🌿';
                text = 'Branch: ' + entry.name;
            } else if (entry.type === 'commit') {
                icon = '✅';
                text = 'Commit created';
            } else if (entry.type === 'file') {
                icon = entry.action === 'created' ? '📄' : '✏️';
                text = (entry.action === 'created' ? 'Created: ' : 'Modified: ') + entry.path.split('/').pop();
            } else if (entry.type === 'subagent') {
                icon = getSubagentIcon(entry.name);
                text = 'Sub-agent: ' + entry.name;
            }

            li.innerHTML = '<span style="margin-right: 8px;">' + icon + '</span>' +
                           '<span class="pr-title" style="font-size: 12px;">' + text + '</span>';
            activityLog.insertBefore(li, activityLog.firstChild);

            // Keep only last 20 entries
            while (activityLog.children.length > 20) {
                activityLog.removeChild(activityLog.lastChild);
            }
        }

        function formatNumber(num) {
            if (num >= 1000000) return (num / 1000000).toFixed(1) + 'M';
            if (num >= 1000) return (num / 1000).toFixed(1) + 'K';
            return num.toString();
        }

        socket.on('pr_created', function(pr) {
            addPR(pr);
        });

        function updateUI(state) {
            document.getElementById('statusBadge').textContent = state.running ? 'Running' : 'Stopped';
            document.getElementById('statusBadge').className = 'status-badge ' + (state.running ? 'status-running' : 'status-stopped');
            document.getElementById('currentCycle').textContent = state.current_cycle || 0;
            document.getElementById('cyclesCompleted').textContent = state.cycles_completed || 0;
            document.getElementById('prsCreated').textContent = state.prs_created ? state.prs_created.length : 0;

            document.getElementById('startBtn').disabled = state.running;
            document.getElementById('stopBtn').disabled = !state.running;

            if (state.project_path) {
                document.getElementById('projectPath').value = state.project_path;
            }

            // Update stage highlights and statuses
            var stages = ['implement', 'test', 'review', 'plan'];
            var stageOrder = {'implement': 0, 'test': 1, 'review': 2, 'plan': 3};
            var currentIdx = state.current_stage ? stageOrder[state.current_stage] : -1;

            stages.forEach(function(s, idx) {
                var stageEl = document.getElementById('stage-' + s);
                var statusEl = document.getElementById('status-' + s);
                stageEl.className = 'stage-item';

                if (state.current_stage === s) {
                    stageEl.classList.add('active');
                    statusEl.textContent = 'Running...';
                    statusEl.className = 'stage-status running';
                } else if (currentIdx > idx) {
                    stageEl.classList.add('completed');
                    statusEl.textContent = 'Done';
                    statusEl.className = 'stage-status done';
                } else {
                    stageEl.classList.add('idle');
                    statusEl.textContent = 'Idle';
                    statusEl.className = 'stage-status';
                }
            });

            if (state.running && state.start_time) {
                startTime = new Date(state.start_time);
                maxSeconds = state.max_hours ? state.max_hours * 3600 : null;
                if (!timerInterval) {
                    timerInterval = setInterval(updateTimer, 1000);
                }
            } else {
                if (timerInterval) {
                    clearInterval(timerInterval);
                    timerInterval = null;
                }
            }

            // Update PRs
            if (state.prs_created && state.prs_created.length > 0) {
                var prList = document.getElementById('prList');
                prList.textContent = '';
                state.prs_created.forEach(function(pr) { addPR(pr, false); });
            }

            // Restore log lines when switching projects
            if (state.log_lines && state.log_lines.length > 0) {
                var logContent = document.getElementById('logContent');
                logContent.innerHTML = '';
                state.log_lines.forEach(function(line) {
                    addLogLine(line);
                });
            }

            // Restore activity stats
            if (state.branches_created !== undefined) {
                document.getElementById('branchesCreated').textContent = state.branches_created || 0;
            }
            if (state.current_branch) {
                document.getElementById('currentBranch').textContent = state.current_branch;
            }
            if (state.files_changed !== undefined) {
                document.getElementById('filesChanged').textContent = state.files_changed || 0;
            }
            if (state.last_file) {
                document.getElementById('lastFile').textContent = state.last_file.split('/').pop();
            }
            if (state.subagent_count !== undefined) {
                document.getElementById('subAgentCount').textContent = state.subagent_count || 0;
            }
            if (state.active_subagent) {
                document.getElementById('activeSubAgent').textContent = state.active_subagent;
            }
            if (state.tools_used !== undefined) {
                document.getElementById('toolsUsed').textContent = state.tools_used || 0;
            }
            if (state.last_tool) {
                document.getElementById('lastTool').textContent = state.last_tool;
            }

            // Restore activity log
            if (state.activity_log && state.activity_log.length > 0) {
                var activityLog = document.getElementById('activityLog');
                activityLog.innerHTML = '';
                state.activity_log.forEach(function(entry) {
                    var li = document.createElement('li');
                    li.className = 'pr-item';
                    li.innerHTML = '<span style="color: #58a6ff;">' + entry.type + '</span> ' + entry.message;
                    activityLog.appendChild(li);
                });
            }

            // Restore sub-agents list
            if (state.subagents_used && state.subagents_used.length > 0) {
                var subagentsList = document.getElementById('subagentsList');
                subagentsList.innerHTML = '';
                state.subagents_used.forEach(function(agent) {
                    var li = document.createElement('li');
                    li.className = 'pr-item';
                    li.textContent = agent;
                    subagentsList.appendChild(li);
                });
            }
        }

        function updateTimer() {
            if (!startTime) return;
            var elapsed = Math.floor((Date.now() - startTime) / 1000);
            var hours = Math.floor(elapsed / 3600);
            var mins = Math.floor((elapsed % 3600) / 60);
            var secs = elapsed % 60;

            if (hours > 0) {
                document.getElementById('timeElapsed').textContent =
                    hours + ':' + String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
            } else {
                document.getElementById('timeElapsed').textContent =
                    String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
            }

            if (maxSeconds) {
                var remaining = Math.max(0, maxSeconds - elapsed);
                var remMins = Math.floor(remaining / 60);
                document.getElementById('timeRemaining').textContent = remMins + ' min remaining';
            } else {
                document.getElementById('timeRemaining').textContent = 'Running indefinitely';
            }
        }

        function addLogLine(line) {
            var logContent = document.getElementById('logContent');
            var div = document.createElement('div');
            div.className = 'log-line new-line';

            // Determine line type and set appropriate class
            if (line.indexOf('[TOOL]') !== -1) {
                div.className += ' log-line-tool';
            } else if (line.indexOf('[STAGE') !== -1) {
                div.className += ' log-line-stage';
            } else if (line.indexOf('error') !== -1 || line.indexOf('Error') !== -1 || line.indexOf('failed') !== -1) {
                div.className += ' log-line-error';
            } else {
                div.className += ' log-line-normal';
            }

            div.textContent = line;
            logContent.appendChild(div);

            // Force scroll to bottom with a small delay for rendering
            requestAnimationFrame(function() {
                logContent.scrollTop = logContent.scrollHeight;
            });

            // Remove highlight class after animation
            setTimeout(function() {
                div.classList.remove('new-line');
            }, 1000);

            // Limit visible log lines to prevent browser slowdown (keep last 1000)
            while (logContent.children.length > 1000) {
                logContent.removeChild(logContent.firstChild);
            }
        }

        function addPR(pr, append) {
            if (append === undefined) append = true;
            var prList = document.getElementById('prList');
            if (append && prList.children.length === 1 && prList.children[0].textContent.indexOf('No PRs') !== -1) {
                prList.textContent = '';
            }
            var li = document.createElement('li');
            li.className = 'pr-item';

            var numSpan = document.createElement('span');
            numSpan.className = 'pr-number';
            numSpan.textContent = '#' + pr.number;

            var titleSpan = document.createElement('span');
            titleSpan.className = 'pr-title';
            titleSpan.textContent = pr.title;

            var link = document.createElement('a');
            link.className = 'pr-link';
            link.href = pr.url;
            link.target = '_blank';
            link.textContent = 'View';

            li.appendChild(numSpan);
            li.appendChild(titleSpan);
            li.appendChild(link);
            prList.appendChild(li);
            document.getElementById('prsCreated').textContent = prList.children.length;
        }

        var loadedTasks = [];

        function loadTodos() {
            var projectPath = document.getElementById('projectPath').value;
            if (!projectPath) {
                alert('Please enter a project path first');
                return;
            }
            socket.emit('load_todos', { project_path: projectPath });
        }

        socket.on('todos_loaded', function(data) {
            loadedTasks = data.tasks;
            var taskList = document.getElementById('taskList');
            taskList.textContent = '';

            if (loadedTasks.length === 0) {
                var empty = document.createElement('div');
                empty.className = 'task-item';
                empty.style.color = '#8b949e';
                empty.style.justifyContent = 'center';
                empty.textContent = 'No incomplete tasks found';
                taskList.appendChild(empty);
                return;
            }

            loadedTasks.forEach(function(task, idx) {
                var div = document.createElement('div');
                div.className = 'task-item priority-' + task.priority;
                div.id = 'task-' + idx;

                var checkbox = document.createElement('input');
                checkbox.type = 'checkbox';
                checkbox.id = 'check-' + idx;
                checkbox.onchange = function() {
                    div.classList.toggle('selected', this.checked);
                    updateQueueCount();
                };

                var label = document.createElement('label');
                label.htmlFor = 'check-' + idx;
                label.textContent = task.text;

                div.appendChild(checkbox);
                div.appendChild(label);
                taskList.appendChild(div);
            });
            updateQueueCount();
        });

        function selectAll() {
            document.querySelectorAll('#taskList input[type="checkbox"]').forEach(function(cb) {
                cb.checked = true;
                cb.parentElement.classList.add('selected');
            });
            updateQueueCount();
        }

        function clearSelection() {
            document.querySelectorAll('#taskList input[type="checkbox"]').forEach(function(cb) {
                cb.checked = false;
                cb.parentElement.classList.remove('selected');
            });
            updateQueueCount();
        }

        function updateQueueCount() {
            var count = document.querySelectorAll('#taskList input[type="checkbox"]:checked').length;
            document.getElementById('queueCount').textContent = count + ' selected';
        }

        function getSelectedTasks() {
            var selected = [];
            document.querySelectorAll('#taskList input[type="checkbox"]:checked').forEach(function(cb, idx) {
                var taskIdx = parseInt(cb.id.replace('check-', ''));
                if (loadedTasks[taskIdx]) {
                    selected.push(loadedTasks[taskIdx].text);
                }
            });
            return selected;
        }

        function startOrchestra() {
            var projectPath = document.getElementById('projectPath').value;
            var maxHours = document.getElementById('maxHours').value;
            var taskMode = document.getElementById('taskMode').value;
            var model = document.getElementById('modelSelect').value;
            var guidance = document.getElementById('initialGuidance').value;
            var selectedTasks = getSelectedTasks();
            var useSubAgents = document.getElementById('useSubAgents').checked;

            if (!projectPath) {
                alert('Please enter a project path');
                return;
            }

            // Generate project ID from path
            var projectId = projectPath.split('/').filter(Boolean).pop() || 'project';

            // Remove from pending projects if this was a pending project
            if (currentProjectId && currentProjectId.startsWith('pending_')) {
                delete pendingProjects[currentProjectId];
            }

            socket.emit('start_orchestra', {
                project_id: projectId,
                project_path: projectPath,
                max_hours: parseFloat(maxHours),
                task_mode: taskMode,
                model: model,
                guidance: guidance,
                task_queue: selectedTasks,
                use_subagents: useSubAgents
            });

            // Switch to this project tab
            currentProjectId = projectId;
            updateProjectTabs();
        }

        function stopOrchestra() {
            if (currentProjectId && !currentProjectId.startsWith('pending_')) {
                socket.emit('stop_project', { project_id: currentProjectId });
            }
        }

        function openBrowser() {
            console.log('openBrowser called');
            var modal = document.getElementById('dirBrowserModal');
            console.log('Modal element:', modal);
            if (modal) {
                modal.classList.add('active');
                console.log('Added active class');
            }
            var startPath = document.getElementById('projectPath').value || '/Users';
            navigateTo(startPath);
        }

        function closeBrowser() {
            document.getElementById('dirBrowserModal').classList.remove('active');
        }

        function navigateTo(path) {
            document.getElementById('currentPath').value = path;
            document.getElementById('dirList').innerHTML = '<div class="dir-item"><span class="dir-name" style="color: #8b949e;">Loading...</span></div>';

            fetch('/api/list-dirs?path=' + encodeURIComponent(path))
                .then(response => response.json())
                .then(data => {
                    var dirList = document.getElementById('dirList');
                    dirList.innerHTML = '';

                    if (data.error) {
                        dirList.innerHTML = '<div class="dir-item"><span class="dir-name" style="color: #f85149;">' + data.error + '</span></div>';
                        return;
                    }

                    // Add parent directory option
                    if (path !== '/') {
                        var parentDiv = document.createElement('div');
                        parentDiv.className = 'dir-item';
                        parentDiv.innerHTML = '<span class="dir-icon">📁</span><span class="dir-name">..</span>';
                        parentDiv.onclick = function() {
                            var parts = path.split('/').filter(p => p);
                            parts.pop();
                            navigateTo('/' + parts.join('/'));
                        };
                        dirList.appendChild(parentDiv);
                    }

                    // Add directories - single click navigates into folder
                    (data.dirs || []).forEach(function(dir) {
                        var div = document.createElement('div');
                        div.className = 'dir-item';
                        div.innerHTML = '<span class="dir-icon">📁</span><span class="dir-name">' + dir + '</span>';
                        div.onclick = function() {
                            navigateTo(path + (path.endsWith('/') ? '' : '/') + dir);
                        };
                        dirList.appendChild(div);
                    });

                    if ((data.dirs || []).length === 0 && path !== '/') {
                        dirList.innerHTML += '<div class="dir-item"><span class="dir-name" style="color: #8b949e;">No subdirectories</span></div>';
                    }
                })
                .catch(err => {
                    document.getElementById('dirList').innerHTML = '<div class="dir-item"><span class="dir-name" style="color: #f85149;">Error loading directory</span></div>';
                });
        }

        function handlePathInput(event) {
            if (event.key === 'Enter') {
                navigateTo(event.target.value);
            }
        }

        function selectCurrentDir() {
            var path = document.getElementById('currentPath').value;
            document.getElementById('projectPath').value = path;
            // Save to pending project if applicable
            if (currentProjectId && currentProjectId.startsWith('pending_') && pendingProjects[currentProjectId]) {
                pendingProjects[currentProjectId].path = path;
                updateProjectTabs();  // Update tab name to show folder name
            }
            closeBrowser();
            loadTodos();
        }

        // ============================================
        // Usage Stats & Rate Limit Handling
        // ============================================
        var rateLimitInterval = null;
        var currentSummaryTab = 'events';
        var DAILY_LIMIT = 20;
        var WEEKLY_LIMIT = 100;

        // Request usage stats on page load
        setTimeout(function() {
            socket.emit('get_usage');
            socket.emit('get_queue');
            socket.emit('get_summary');
        }, 500);

        socket.on('usage_update', function(data) {
            updateUsageUI(data);
        });

        function updateUsageUI(data) {
            var today = data.requests_today || 0;
            var week = data.requests_this_week || 0;
            var tokens = data.tokens_estimated || 0;

            document.getElementById('usageToday').textContent = today + '/' + DAILY_LIMIT;
            document.getElementById('usageWeek').textContent = week + '/' + WEEKLY_LIMIT;
            document.getElementById('usageTokens').textContent = formatNumber(tokens);

            // Update progress bar
            var percentage = Math.min((today / DAILY_LIMIT) * 100, 100);
            var progressFill = document.getElementById('usageProgressFill');
            progressFill.style.width = percentage + '%';
            progressFill.className = 'usage-progress-fill';
            if (percentage >= 90) {
                progressFill.classList.add('danger');
            } else if (percentage >= 70) {
                progressFill.classList.add('warning');
            }

            // Handle rate limit
            if (data.rate_limited && data.rate_limit_until) {
                showRateLimitWarning(data.rate_limit_until);
            } else {
                hideRateLimitWarning();
            }
        }

        function showRateLimitWarning(untilTime) {
            var warning = document.getElementById('rateLimitWarning');
            warning.classList.add('active');

            if (rateLimitInterval) clearInterval(rateLimitInterval);
            rateLimitInterval = setInterval(function() {
                var now = Date.now();
                var until = new Date(untilTime).getTime();
                var remaining = Math.max(0, until - now);

                if (remaining <= 0) {
                    hideRateLimitWarning();
                    socket.emit('clear_rate_limit');
                    return;
                }

                var mins = Math.floor(remaining / 60000);
                var secs = Math.floor((remaining % 60000) / 1000);
                document.getElementById('rateLimitCountdown').textContent =
                    String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');
            }, 1000);
        }

        function hideRateLimitWarning() {
            document.getElementById('rateLimitWarning').classList.remove('active');
            if (rateLimitInterval) {
                clearInterval(rateLimitInterval);
                rateLimitInterval = null;
            }
        }

        function clearRateLimit() {
            socket.emit('clear_rate_limit');
        }

        // ============================================
        // Message Queue Handling
        // ============================================
        socket.on('queue_update', function(data) {
            updateMessageQueueUI(data);
        });

        function updateMessageQueueUI(data) {
            var queue = data.queue || [];
            document.getElementById('messageQueueCount').textContent = queue.length;

            var queueList = document.getElementById('messageQueueList');
            // Clear using DOM methods
            while (queueList.firstChild) {
                queueList.removeChild(queueList.firstChild);
            }

            if (queue.length === 0) {
                var emptyMsg = document.createElement('div');
                emptyMsg.style.cssText = 'color: #8b949e; text-align: center; padding: 10px; font-size: 12px;';
                emptyMsg.textContent = 'No messages queued';
                queueList.appendChild(emptyMsg);
                return;
            }

            queue.forEach(function(item) {
                var div = document.createElement('div');
                div.className = 'queue-item';

                var statusSpan = document.createElement('span');
                statusSpan.className = 'queue-status ' + item.status;

                var textSpan = document.createElement('span');
                textSpan.className = 'queue-text';
                textSpan.textContent = item.message.substring(0, 100) + (item.message.length > 100 ? '...' : '');

                var removeSpan = document.createElement('span');
                removeSpan.className = 'queue-remove';
                removeSpan.textContent = '×';
                removeSpan.onclick = function() { removeFromQueue(item.id); };

                div.appendChild(statusSpan);
                div.appendChild(textSpan);
                div.appendChild(removeSpan);
                queueList.appendChild(div);
            });
        }

        function addToMessageQueue() {
            var input = document.getElementById('queueMessageInput');
            var message = input.value.trim();
            if (!message) {
                alert('Please enter a message to queue');
                return;
            }

            socket.emit('add_to_queue', {
                message: message,
                project_id: currentProjectId && !currentProjectId.startsWith('pending_') ? currentProjectId : null
            });
            input.value = '';
        }

        function removeFromQueue(id) {
            socket.emit('remove_from_queue', { id: id });
        }

        // ============================================
        // Summary/Master View Handling
        // ============================================
        socket.on('summary_update', function(data) {
            updateSummaryUI(data);
        });

        function updateSummaryUI(data) {
            document.getElementById('summaryRequests').textContent = data.total_requests || 0;
            document.getElementById('summaryTokens').textContent = formatNumber(data.total_tokens || 0);
            document.getElementById('summaryProjects').textContent = Object.keys(projectsData).length;
            document.getElementById('summaryAlerts').textContent = data.alerts || 0;
            renderSummaryContent(data);
        }

        function switchSummaryTab(tab) {
            currentSummaryTab = tab;
            document.querySelectorAll('.summary-tab').forEach(function(el) {
                el.classList.remove('active');
                if (el.textContent.toLowerCase() === tab) {
                    el.classList.add('active');
                }
            });
            socket.emit('get_summary');
        }

        function renderSummaryContent(data) {
            var content = document.getElementById('summaryContent');
            // Clear content safely
            while (content.firstChild) {
                content.removeChild(content.firstChild);
            }

            if (currentSummaryTab === 'events') {
                var events = data.events || [];
                if (events.length === 0) {
                    var empty = document.createElement('div');
                    empty.style.cssText = 'color: #8b949e; text-align: center; padding: 20px;';
                    empty.textContent = 'No events yet';
                    content.appendChild(empty);
                    return;
                }
                events.slice(0, 50).forEach(function(event) {
                    var div = document.createElement('div');
                    div.className = 'summary-event';

                    var timeSpan = document.createElement('span');
                    timeSpan.className = 'event-time';
                    timeSpan.textContent = new Date(event.timestamp).toLocaleTimeString();

                    var typeSpan = document.createElement('span');
                    typeSpan.className = 'event-type';
                    typeSpan.textContent = event.type || 'info';

                    var msgSpan = document.createElement('span');
                    msgSpan.className = 'event-message';
                    msgSpan.textContent = event.message || '';

                    div.appendChild(timeSpan);
                    div.appendChild(typeSpan);
                    div.appendChild(msgSpan);
                    content.appendChild(div);
                });
            } else if (currentSummaryTab === 'hourly') {
                var hourly = data.hourly || {};
                var hours = Object.keys(hourly).sort().reverse();
                if (hours.length === 0) {
                    var empty = document.createElement('div');
                    empty.style.cssText = 'color: #8b949e; text-align: center; padding: 20px;';
                    empty.textContent = 'No hourly data yet';
                    content.appendChild(empty);
                    return;
                }
                hours.slice(0, 24).forEach(function(hour) {
                    var stats = hourly[hour];
                    var div = document.createElement('div');
                    div.className = 'summary-event';

                    var timeSpan = document.createElement('span');
                    timeSpan.className = 'event-time';
                    timeSpan.textContent = hour + ':00';

                    var typeSpan = document.createElement('span');
                    typeSpan.className = 'event-type';
                    typeSpan.textContent = 'requests';

                    var msgSpan = document.createElement('span');
                    msgSpan.className = 'event-message';
                    msgSpan.textContent = stats.requests + ' requests, ' + formatNumber(stats.tokens) + ' tokens';

                    div.appendChild(timeSpan);
                    div.appendChild(typeSpan);
                    div.appendChild(msgSpan);
                    content.appendChild(div);
                });
            } else if (currentSummaryTab === 'daily') {
                var daily = data.daily || {};
                var days = Object.keys(daily).sort().reverse();
                if (days.length === 0) {
                    var empty = document.createElement('div');
                    empty.style.cssText = 'color: #8b949e; text-align: center; padding: 20px;';
                    empty.textContent = 'No daily data yet';
                    content.appendChild(empty);
                    return;
                }
                days.slice(0, 7).forEach(function(day) {
                    var stats = daily[day];
                    var div = document.createElement('div');
                    div.className = 'summary-event';

                    var timeSpan = document.createElement('span');
                    timeSpan.className = 'event-time';
                    timeSpan.textContent = day;

                    var typeSpan = document.createElement('span');
                    typeSpan.className = 'event-type';
                    typeSpan.textContent = 'daily';

                    var msgSpan = document.createElement('span');
                    msgSpan.className = 'event-message';
                    msgSpan.textContent = stats.requests + ' requests, ' + formatNumber(stats.tokens) + ' tokens';

                    div.appendChild(timeSpan);
                    div.appendChild(typeSpan);
                    div.appendChild(msgSpan);
                    content.appendChild(div);
                });
            }
        }

        // Request updates periodically
        setInterval(function() {
            socket.emit('get_usage');
            socket.emit('get_summary');
        }, 30000);

        // ========================================
        // Multi-User Mode Functions
        // ========================================

        // Multi-User Mode State
        let multiuserConfig = { configured: false };
        let claimsData = { claims: [] };
        let availableTasksData = { tasks: [] };

        // Toggle panel
        function toggleMultiUserPanel() {
            const content = document.getElementById('multiuser-content');
            const toggle = document.getElementById('multiuser-toggle');
            content.classList.toggle('expanded');
            toggle.textContent = content.classList.contains('expanded') ? '▲' : '▼';

            if (content.classList.contains('expanded')) {
                socket.emit('get_multiuser_config');
                refreshClaims();

                // Auto-detect repo from current project
                const projectPath = getCurrentProjectPath();
                if (projectPath) {
                    socket.emit('get_repo_from_project', { project_path: projectPath });
                }
            }
        }

        // Get current project path from either running or pending projects
        function getCurrentProjectPath() {
            // Check running projects first
            if (projectsData[currentProjectId]) {
                return projectsData[currentProjectId].project_path;
            }
            // Check pending projects
            if (pendingProjects[currentProjectId]) {
                return pendingProjects[currentProjectId].path;
            }
            return null;
        }

        // Socket handlers for multi-user mode
        socket.on('multiuser_config', function(data) {
            multiuserConfig = data;
            updateConfigUI();
        });

        // Handle auto-detected repo from project
        socket.on('project_repo', function(data) {
            if (data.success && data.repo) {
                const repoInput = document.getElementById('github-repo');
                // Only auto-fill if empty or same as before
                if (!repoInput.value || repoInput.value === multiuserConfig.repo) {
                    repoInput.value = data.repo;
                }
            }
        });

        socket.on('connection_result', function(data) {
            const status = document.getElementById('connection-status');
            const tokenInput = document.getElementById('github-token');

            if (data.success) {
                status.className = 'connection-status success';
                status.textContent = '✓ Connected as @' + data.username;
                tokenInput.classList.add('success');
                tokenInput.classList.remove('error');
            } else {
                status.className = 'connection-status error';
                status.textContent = '✗ ' + data.error;
                tokenInput.classList.add('error');
                tokenInput.classList.remove('success');
            }

            document.getElementById('test-btn').disabled = false;
            document.getElementById('test-btn').textContent = '🔌 Test Connection';
        });

        socket.on('config_saved', function(data) {
            document.getElementById('save-btn').disabled = false;
            document.getElementById('save-btn').textContent = '💾 Save & Enable';
            if (data.success) {
                refreshClaims();
            }
        });

        socket.on('sync_result', function(data) {
            const btn = document.getElementById('sync-btn');
            const results = document.getElementById('sync-results');

            btn.disabled = false;
            btn.textContent = '🔄 Sync Now';

            if (data.success) {
                results.classList.remove('hidden');
                document.getElementById('sync-created').textContent = data.created;
                document.getElementById('sync-updated').textContent = data.updated;
                document.getElementById('sync-unchanged').textContent = data.unchanged;
            } else {
                alert('Sync failed: ' + data.error);
            }
        });

        socket.on('claims_update', function(data) {
            claimsData = data;
            renderClaims();
        });

        socket.on('available_tasks_update', function(data) {
            availableTasksData = data;
            renderAvailableTasks();
        });

        socket.on('stale_reclaimed', function(data) {
            if (data.success) {
                alert('Released ' + data.released_count + ' stale claim(s)');
            }
        });

        // Multi-User UI Functions
        function updateConfigUI() {
            const status = document.getElementById('multiuser-status');

            if (multiuserConfig.configured) {
                status.textContent = 'Enabled';
                status.className = 'multiuser-status configured';
            } else {
                status.textContent = 'Not Configured';
                status.className = 'multiuser-status not-configured';
            }

            // Pre-fill form if we have config
            if (multiuserConfig.repo) {
                document.getElementById('github-repo').value = multiuserConfig.repo;
            }
            if (multiuserConfig.claim_timeout) {
                document.getElementById('claim-timeout').value = multiuserConfig.claim_timeout;
            }
            if (multiuserConfig.heartbeat_interval) {
                document.getElementById('heartbeat-interval').value = multiuserConfig.heartbeat_interval;
            }
            if (multiuserConfig.has_token) {
                document.getElementById('github-token').placeholder = multiuserConfig.masked_token || 'Token saved';
            }
        }

        function testConnection() {
            const btn = document.getElementById('test-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner"></span> Testing...';

            const token = document.getElementById('github-token').value;
            if (token) {
                socket.emit('save_multiuser_config', {
                    github_token: token,
                    repo: document.getElementById('github-repo').value,
                    claim_timeout: document.getElementById('claim-timeout').value,
                    heartbeat_interval: document.getElementById('heartbeat-interval').value
                });
            }

            socket.emit('test_github_connection');
        }

        function saveConfig() {
            const btn = document.getElementById('save-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner"></span> Saving...';

            socket.emit('save_multiuser_config', {
                github_token: document.getElementById('github-token').value,
                repo: document.getElementById('github-repo').value,
                claim_timeout: document.getElementById('claim-timeout').value,
                heartbeat_interval: document.getElementById('heartbeat-interval').value
            });
        }

        function syncTodos() {
            const btn = document.getElementById('sync-btn');
            btn.disabled = true;
            btn.innerHTML = '<span class="spinner"></span> Syncing...';

            // Include project path so sync knows where TODO.md is
            const projectPath = getCurrentProjectPath();
            socket.emit('sync_todos', { project_path: projectPath });
        }

        function refreshClaims() {
            socket.emit('get_claims');
            socket.emit('get_available_tasks');
        }

        function reclaimStale() {
            if (confirm('Release all stale claims (no heartbeat for 30+ min)?')) {
                socket.emit('reclaim_stale');
            }
        }

        function releaseClaim(issueNumber) {
            if (confirm('Release your claim on issue #' + issueNumber + '?')) {
                socket.emit('release_claim', { issue_number: issueNumber });
            }
        }

        function renderClaims() {
            const list = document.getElementById('claims-list');
            const count = document.getElementById('claims-count');

            count.textContent = claimsData.claims ? claimsData.claims.length : 0;

            if (!claimsData.claims || claimsData.claims.length === 0) {
                list.innerHTML = '<li style="color: #6e7681; font-size: 11px;">No active claims</li>';
                return;
            }

            list.innerHTML = claimsData.claims.map(claim => {
                const isMine = claim.is_mine;
                const age = claim.age_minutes || 0;
                const isStale = age > 30;
                const isWarning = age > 15;
                const heartbeatClass = isStale ? 'stale' : (isWarning ? 'warning' : 'fresh');
                const ageText = age < 1 ? 'just now' : age + 'm ago';

                return '<li class="claim-item ' + (isMine ? 'mine' : '') + ' ' + (isStale ? 'stale' : '') + '">' +
                    '<div class="claim-issue">#' + claim.issue_number + (isMine ? ' (you)' : '') + '</div>' +
                    '<div class="claim-agent">' + claim.agent_id.substring(0, 25) + '...</div>' +
                    '<div class="claim-meta">' +
                        '<span class="heartbeat-indicator"><span class="heartbeat-dot ' + heartbeatClass + '"></span>' + ageText + '</span>' +
                        (isMine ? '<button class="claim-release-btn" onclick="releaseClaim(' + claim.issue_number + ')">Release</button>' : '') +
                    '</div>' +
                '</li>';
            }).join('');
        }

        function renderAvailableTasks() {
            const list = document.getElementById('available-tasks-list');
            const count = document.getElementById('available-count');

            if (!availableTasksData.tasks || availableTasksData.tasks.length === 0) {
                count.textContent = '0';
                list.innerHTML = '<div style="color: #6e7681; font-size: 11px;">No available tasks</div>';
                return;
            }

            count.textContent = availableTasksData.tasks.length;

            list.innerHTML = availableTasksData.tasks.slice(0, 5).map(task => {
                const priority = task.priority ? '<span class="task-label ' + task.priority + '">' + task.priority + '</span>' : '';
                const size = task.size ? '<span class="task-label ' + task.size + '">' + task.size + '</span>' : '';
                const title = task.title.length > 35 ? task.title.substring(0, 35) + '...' : task.title;

                return '<div class="task-item">' +
                    '<span class="task-number">#' + task.issue_number + '</span>' +
                    '<span class="task-title">' + escapeHtml(title) + '</span>' +
                    '<div class="task-labels">' + priority + size + '</div>' +
                '</div>';
            }).join('');
        }

        function escapeHtml(text) {
            const div = document.createElement('div');
            div.textContent = text;
            return div.innerHTML;
        }

        // Auto-refresh claims every 30s when panel is open
        setInterval(function() {
            if (document.getElementById('multiuser-content') &&
                document.getElementById('multiuser-content').classList.contains('expanded')) {
                refreshClaims();
            }
        }, 30000);
    </script>

    <!-- Directory Browser Modal -->
    <div class="modal-overlay" id="dirBrowserModal" onclick="if(event.target===this)closeBrowser()">
        <div class="modal">
            <div class="modal-header">
                <h2>Select Project Directory</h2>
                <button class="modal-close" onclick="closeBrowser()">&times;</button>
            </div>
            <div class="modal-path">
                <span>📂</span>
                <input type="text" id="currentPath" value="/" onkeypress="handlePathInput(event)">
            </div>
            <div class="modal-content" id="dirList">
            </div>
            <div class="modal-footer">
                <button onclick="closeBrowser()">Cancel</button>
                <button class="primary" onclick="selectCurrentDir()">Select This Folder</button>
            </div>
        </div>
    </div>
</body>
</html>
"""

def parse_todo_file(project_path):
    """Parse TODO.md and extract incomplete tasks."""
    import re
    tasks = []
    todo_files = [
        'TODO.md',
        'docs/TODO.md',
        'docs/TASKS.md',
        'docs/WORK_ALLOCATION.md',
        '.github/TODO.md',
        'TASKS.md'
    ]

    for todo_file in todo_files:
        todo_path = os.path.join(project_path, todo_file)
        if not os.path.exists(todo_path):
            continue

        try:
            with open(todo_path, 'r') as f:
                content = f.read()

            current_priority = 'medium'
            for line in content.split('\n'):
                # Detect priority sections
                line_lower = line.lower()
                if 'high priority' in line_lower or '## high' in line_lower:
                    current_priority = 'high'
                elif 'medium priority' in line_lower or '## medium' in line_lower:
                    current_priority = 'medium'
                elif 'low priority' in line_lower or '## low' in line_lower:
                    current_priority = 'low'

                # Find incomplete tasks (- [ ] or * [ ])
                match = re.match(r'^[\s]*[-*]\s*\[\s*\]\s*(.+)$', line)
                if match:
                    task_text = match.group(1).strip()
                    if task_text and len(task_text) > 3:
                        tasks.append({
                            'text': task_text,
                            'priority': current_priority,
                            'source': todo_file
                        })
        except Exception as e:
            pass

    return tasks

@socketio.on('load_todos')
def handle_load_todos(data):
    project_path = data.get('project_path')
    if not project_path or not os.path.exists(project_path):
        emit('todos_loaded', {'tasks': [], 'error': 'Invalid path'})
        return

    tasks = parse_todo_file(project_path)
    emit('todos_loaded', {'tasks': tasks})

def check_prs(project_path, socketio):
    """Periodically check for new PRs."""
    known_prs = set()
    while orchestra_state["running"]:
        try:
            result = subprocess.run(
                ['gh', 'pr', 'list', '--json', 'number,title,url', '--limit', '20'],
                cwd=project_path,
                capture_output=True,
                text=True
            )
            if result.returncode == 0:
                prs = json.loads(result.stdout)
                for pr in prs:
                    if pr['number'] not in known_prs:
                        known_prs.add(pr['number'])
                        pr_data = {
                            'number': pr['number'],
                            'title': pr['title'],
                            'url': pr['url']
                        }
                        orchestra_state["prs_created"].append(pr_data)
                        socketio.emit('pr_created', pr_data)
                        socketio.emit('state_update', get_serializable_state())
        except Exception as e:
            pass
        time.sleep(30)

RECENT_PROJECTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '.recent_projects.json')

def load_recent_projects():
    """Load recent projects from file."""
    try:
        if os.path.exists(RECENT_PROJECTS_FILE):
            with open(RECENT_PROJECTS_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return []

def save_recent_project(path):
    """Add a project to recent projects list."""
    projects = load_recent_projects()
    # Remove if already exists (to move to front)
    if path in projects:
        projects.remove(path)
    # Add to front
    projects.insert(0, path)
    # Keep only last 10
    projects = projects[:10]
    try:
        with open(RECENT_PROJECTS_FILE, 'w') as f:
            json.dump(projects, f)
    except:
        pass
    return projects

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/state')
def get_state():
    return jsonify(get_serializable_state())

@app.route('/api/recent-projects')
def get_recent_projects():
    """Get list of recent projects."""
    return jsonify({'projects': load_recent_projects()})

@app.route('/api/recent-projects', methods=['POST'])
def add_recent_project():
    """Add a project to recent list."""
    data = request.get_json() or {}
    path = data.get('path', '')
    if path and os.path.exists(path):
        projects = save_recent_project(path)
        return jsonify({'projects': projects})
    return jsonify({'error': 'Invalid path'}), 400

@app.route('/api/list-dirs')
def list_dirs():
    """List directories in a given path."""
    path = request.args.get('path', '/')

    # Normalize path
    path = os.path.normpath(path)
    if not path.startswith('/'):
        path = '/' + path

    if not os.path.exists(path):
        return jsonify({'error': 'Path does not exist', 'dirs': []})

    if not os.path.isdir(path):
        return jsonify({'error': 'Not a directory', 'dirs': []})

    try:
        # List only directories, sorted alphabetically
        dirs = []
        for item in sorted(os.listdir(path)):
            # Skip hidden files/folders
            if item.startswith('.'):
                continue
            full_path = os.path.join(path, item)
            if os.path.isdir(full_path):
                dirs.append(item)
        return jsonify({'path': path, 'dirs': dirs})
    except PermissionError:
        return jsonify({'error': 'Permission denied', 'dirs': []})
    except Exception as e:
        return jsonify({'error': str(e), 'dirs': []})

@socketio.on('connect')
def handle_connect():
    emit('state_update', get_serializable_state())
    emit('projects_update', {'projects': get_all_projects_summary()})

@socketio.on('get_state')
def handle_get_state():
    emit('state_update', get_serializable_state())

@socketio.on('get_all_projects')
def handle_get_all_projects():
    emit('projects_update', {'projects': get_all_projects_summary()})

@socketio.on('get_project_state')
def handle_get_project_state(data):
    project_id = data.get('project_id')
    if project_id and project_id in projects_state:
        state = projects_state[project_id]
        emit('project_state', {
            'project_id': project_id,
            'state': get_serializable_state(state)
        })
        emit('state_update', get_serializable_state(state))

@socketio.on('stop_project')
def handle_stop_project(data):
    project_id = data.get('project_id')
    if project_id and project_id in projects_state:
        state = projects_state[project_id]
        state["running"] = False

        # Use process manager to stop gracefully
        success = process_manager.stop_process(project_id, timeout=10)

        if not success and state.get("process"):
            # Fallback to manual termination if process manager fails
            state["process"].terminate()

        emit('state_update', get_serializable_state(state))
        emit('projects_update', {'projects': get_all_projects_summary()})
        emit('log_line', {'line': f'Stopping orchestra for {project_id}...'})

@socketio.on('remove_project')
def handle_remove_project(data):
    project_id = data.get('project_id')
    if project_id and project_id in projects_state:
        state = projects_state[project_id]
        if state.get("running"):
            # Use process manager to stop gracefully
            process_manager.stop_process(project_id, timeout=10)
        del projects_state[project_id]
        emit('projects_update', {'projects': get_all_projects_summary()})

# ============================================
# Usage Stats Socket Handlers
# ============================================
@socketio.on('get_usage')
def handle_get_usage():
    """Send current usage stats to client."""
    emit('usage_update', get_usage_stats())

@socketio.on('clear_rate_limit')
def handle_clear_rate_limit():
    """Clear rate limit status and emit update."""
    clear_rate_limit()
    emit('usage_update', get_usage_stats())

# ============================================
# Message Queue Socket Handlers
# ============================================
@socketio.on('get_queue')
def handle_get_queue():
    """Send current queue to client."""
    emit('queue_update', {'queue': get_queue_status()})

@socketio.on('add_to_queue')
def handle_add_to_queue(data):
    """Add a message to the queue."""
    project_id = data.get('project_id')
    message = data.get('message', '').strip()

    if not project_id or not message:
        return

    item = add_to_queue(message, project_id)
    # Broadcast queue update to all clients
    socketio.emit('queue_update', {'queue': get_queue_status()})
    socketio.emit('log_line', {'line': f'[Queue] Added task for {project_id}: {message[:50]}...'})

# ============================================
# Summary Socket Handlers
# ============================================
@socketio.on('get_summary')
def handle_get_summary(data=None):
    """Get summary stats for the requested time range."""
    time_range = (data or {}).get('time_range', 'today')
    stats = get_summary_stats(time_range)
    emit('summary_update', stats)

# ============================================
# Safeguard Socket Handlers
# ============================================
@socketio.on('get_safeguards')
def handle_get_safeguards():
    """Get current safeguard status."""
    emit('safeguard_status', get_safeguard_status())

@socketio.on('clear_safeguard_alerts')
def handle_clear_safeguard_alerts():
    """Clear safeguard alerts."""
    global safeguards
    safeguards["alerts"] = []
    safeguards["path_violations"] = []
    emit('safeguard_status', get_safeguard_status())
    socketio.emit('log_line', {'line': '[SAFEGUARD] Alerts cleared'})

@socketio.on('start_orchestra')
def handle_start(data):
    global orchestra_state, active_project_id

    project_path = data.get('project_path')
    project_id = data.get('project_id') or get_project_id_from_path(project_path)
    max_hours = data.get('max_hours', 1)
    task_mode = data.get('task_mode', 'normal')
    model = data.get('model', 'sonnet')
    guidance = data.get('guidance', '').strip()
    task_queue = data.get('task_queue', [])
    use_subagents = data.get('use_subagents', True)

    if not project_path or not os.path.exists(project_path):
        emit('log_line', {'line': 'Error: Invalid project path: ' + str(project_path)})
        return

    # Check if this project is already running
    if project_id in projects_state and projects_state[project_id].get("running"):
        emit('log_line', {'line': f'Error: Project {project_id} is already running'})
        return

    # Save to recent projects
    save_recent_project(project_path)

    # Create new project state
    project_state = create_project_state()
    project_state["running"] = True
    project_state["project_id"] = project_id
    project_state["project_path"] = project_path
    project_state["start_time"] = datetime.now().isoformat()
    project_state["max_hours"] = max_hours
    project_state["task_mode"] = task_mode
    project_state["model"] = model
    project_state["guidance"] = guidance
    project_state["task_queue"] = task_queue
    project_state["use_subagents"] = use_subagents

    # Add to projects state
    projects_state[project_id] = project_state
    active_project_id = project_id

    # Also update the global orchestra_state for backwards compatibility
    orchestra_state = project_state

    emit('state_update', get_serializable_state(project_state))
    emit('projects_update', {'projects': get_all_projects_summary()})
    emit('log_line', {'line': f'Starting Claude Orchestra on {project_path} (ID: {project_id})'})
    subagent_status = 'ON' if use_subagents else 'OFF'
    emit('log_line', {'line': 'Task mode: ' + task_mode.upper() + ' | Model: ' + model.upper() + ' | Sub-Agents: ' + subagent_status})
    if guidance:
        emit('log_line', {'line': 'Initial guidance: ' + guidance[:100] + ('...' if len(guidance) > 100 else '')})
    if task_queue:
        emit('log_line', {'line': 'Task queue: ' + str(len(task_queue)) + ' task(s) queued'})
    if max_hours:
        emit('log_line', {'line': 'Max runtime: ' + str(max_hours) + ' hour(s)'})
    else:
        emit('log_line', {'line': 'Running indefinitely (until stopped)'})

    def run_orchestra():
        # Use project_state from closure (project-specific)
        state = project_state
        pid = project_id

        script_dir = os.path.dirname(os.path.abspath(__file__))
        log_file = os.path.join(script_dir, f'claude_orchestra_{pid}.log')

        open(log_file, 'w').close()

        cmd = [
            'python3', '-u',  # Unbuffered output for real-time streaming
            os.path.join(script_dir, 'claude_orchestra.py'),
            '--project', project_path,
            '--continuous',
            '--timeout', '1800',  # 30 minutes per agent
            '--max-cycles', '1000'  # High limit, will stop on time or manual stop
        ]

        # Only add max-hours if not indefinite
        if state["max_hours"]:
            cmd.extend(['--max-hours', str(state["max_hours"])])

        # Add task mode and model
        cmd.extend(['--task-mode', state.get("task_mode", "normal")])
        cmd.extend(['--model', state.get("model", "sonnet")])

        # Add guidance if provided
        if state.get("guidance"):
            cmd.extend(['--guidance', state["guidance"]])

        # Add task queue if provided (as JSON)
        if state.get("task_queue"):
            cmd.extend(['--task-queue', json.dumps(state["task_queue"])])

        # Add sub-agents flag if enabled
        if state.get("use_subagents", True):
            cmd.append('--use-subagents')

        state["process"] = subprocess.Popen(
            cmd,
            cwd=script_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1  # Line buffered for real-time output
        )

        # Track the process for automatic cleanup
        process_manager.track_process(pid, state["process"])

        # Use select for non-blocking reads to allow stop button to work
        import select
        while state["running"]:
            # Check if process ended
            if state["process"].poll() is not None:
                # Process ended, read remaining output
                remaining = state["process"].stdout.read()
                if remaining:
                    for line in remaining.split('\n'):
                        if line.strip():
                            log_text = f'[{pid}] ' + line.strip()
                            state['log_lines'].append(log_text)
                            # Keep last 500 lines to prevent memory bloat
                            if len(state['log_lines']) > 500:
                                state['log_lines'] = state['log_lines'][-500:]
                            socketio.emit('log_line', {'line': log_text, 'project_id': pid})
                break

            # Non-blocking check if data is available (0.5s timeout)
            ready, _, _ = select.select([state["process"].stdout], [], [], 0.5)
            if not ready:
                continue  # No data, loop again to check running flag

            line = state["process"].stdout.readline()
            if line:
                line_text = line.strip()
                log_text = f'[{pid}] ' + line_text
                state['log_lines'].append(log_text)
                # Keep last 500 lines to prevent memory bloat
                if len(state['log_lines']) > 500:
                    state['log_lines'] = state['log_lines'][-500:]
                socketio.emit('log_line', {'line': log_text, 'project_id': pid})

                # Check for rate limit and track usage
                wait_time = check_rate_limit(line_text)
                if wait_time:
                    socketio.emit('log_line', {'line': f'[{pid}] ⚠️ Rate limit detected, auto-resuming in {wait_time}s'})
                    socketio.emit('usage_update', get_usage_stats())

                # Check for cross-repo activity (safeguard)
                current_project_path = state.get('project_path', '')
                if current_project_path:
                    check_cross_repo_activity(line_text, current_project_path, pid)

                # Parse stage transitions
                if '[STAGE 1]' in line_text or 'IMPLEMENTER' in line_text.upper():
                    state["current_stage"] = "implement"
                elif '[STAGE 2]' in line_text or 'TESTER' in line_text.upper():
                    state["current_stage"] = "test"
                elif '[STAGE 3]' in line_text or 'REVIEWER' in line_text.upper():
                    state["current_stage"] = "review"
                elif '[STAGE 4]' in line_text or 'PLANNER' in line_text.upper():
                    state["current_stage"] = "plan"
                elif 'CYCLE' in line_text and '/' in line_text:
                    try:
                        cycle = int(line_text.split('CYCLE')[1].split('/')[0].strip())
                        state["current_cycle"] = cycle
                    except:
                        pass
                elif 'Cycle' in line_text and 'complete' in line_text:
                    state["cycles_completed"] += 1
                    state["current_stage"] = None  # Reset for next cycle
                    # Add to summary
                    add_summary_event('cycle', f'Cycle {state["cycles_completed"]} completed', pid)

                # Parse activity from stream-json events and log output
                try:
                    if line_text.startswith('{') and '"type"' in line_text:
                        event = json.loads(line_text)

                        # Track tool usage from tool_use events
                        if event.get('type') == 'tool_use':
                            tool_name = event.get('name', event.get('tool', 'unknown'))
                            state["tools_used"] += 1
                            state["last_tool"] = tool_name
                            # Track API request
                            track_api_request()

                            # Track file changes
                            if tool_name in ('Edit', 'Write', 'NotebookEdit'):
                                file_path = event.get('input', {}).get('file_path', '')
                                # Check for path traversal (file outside project)
                                current_project_path = state.get('project_path', '')
                                if file_path and current_project_path:
                                    check_path_traversal(file_path, current_project_path, pid)
                                if file_path and file_path not in state["files_changed_set"]:
                                    state["files_changed_set"].add(file_path)
                                    state["files_changed"] = len(state["files_changed_set"])
                                    state["last_file"] = file_path
                                    # Add to activity log and emit
                                    entry = {
                                        'type': 'file',
                                        'action': 'modified' if tool_name == 'Edit' else 'created',
                                        'path': file_path,
                                        'time': datetime.now().isoformat(),
                                        'project_id': pid
                                    }
                                    state["activity_log"].append(entry)
                                    socketio.emit('activity_log_entry', entry)
                                    # Add to summary
                                    add_summary_event('file', f'Modified {file_path.split("/")[-1]}', pid)

                            # Track sub-agent invocations
                            elif tool_name == 'Task':
                                subagent_type = event.get('input', {}).get('subagent_type', 'unknown')
                                state["subagent_count"] += 1
                                state["active_subagent"] = subagent_type
                                if subagent_type not in state["subagents_used"]:
                                    state["subagents_used"].append(subagent_type)
                                # Add to activity log and emit
                                entry = {
                                    'type': 'subagent',
                                    'name': subagent_type,
                                    'time': datetime.now().isoformat(),
                                    'project_id': pid
                                }
                                state["activity_log"].append(entry)
                                socketio.emit('activity_log_entry', entry)

                            # Track branch creation via Bash
                            elif tool_name == 'Bash':
                                cmd = event.get('input', {}).get('command', '')
                                if 'git checkout -b' in cmd or 'git branch ' in cmd:
                                    # Extract branch name
                                    branch_match = re.search(r'(?:checkout -b|branch)\s+([^\s]+)', cmd)
                                    if branch_match:
                                        branch_name = branch_match.group(1)
                                        state["branches_created"] += 1
                                        state["current_branch"] = branch_name
                                        entry = {
                                            'type': 'branch',
                                            'name': branch_name,
                                            'time': datetime.now().isoformat(),
                                            'project_id': pid
                                        }
                                        state["activity_log"].append(entry)
                                        socketio.emit('activity_log_entry', entry)
                                elif 'git commit' in cmd:
                                    entry = {
                                        'type': 'commit',
                                        'time': datetime.now().isoformat(),
                                        'project_id': pid
                                    }
                                    state["activity_log"].append(entry)
                                    socketio.emit('activity_log_entry', entry)

                        # Clear active subagent when task completes
                        elif event.get('type') == 'tool_result':
                            if state["active_subagent"]:
                                state["active_subagent"] = None
                except (json.JSONDecodeError, KeyError):
                    pass

                # Also parse text-based tool indicators [TOOL]
                if '[TOOL]' in line_text:
                    tool_match = re.search(r'\[TOOL\]\s*(\w+)', line_text)
                    if tool_match:
                        tool_name = tool_match.group(1)
                        state["tools_used"] += 1
                        state["last_tool"] = tool_name

                # Emit activity update
                socketio.emit('activity_update', {
                    'project_id': pid,
                    'branches_created': state["branches_created"],
                    'current_branch': state["current_branch"],
                    'files_changed': state["files_changed"],
                    'last_file': state["last_file"],
                    'subagent_count': state["subagent_count"],
                    'active_subagent': state["active_subagent"],
                    'subagents_used': state["subagents_used"],
                    'tools_used': state["tools_used"],
                    'last_tool': state["last_tool"]
                })

                socketio.emit('state_update', get_serializable_state(state))
                socketio.emit('projects_update', {'projects': get_all_projects_summary()})

        state["running"] = False
        state["current_stage"] = None

        # Untrack the process when it completes
        process_manager.untrack_process(pid)

        socketio.emit('state_update', get_serializable_state(state))
        socketio.emit('projects_update', {'projects': get_all_projects_summary()})
        socketio.emit('log_line', {'line': f'[{pid}] Orchestra stopped'})

    def cleanup_orphans():
        """Periodically check for and clean up orphaned Claude processes."""
        # Use project_state and project_id from the enclosing socket handler scope
        while project_state["running"]:
            try:
                # Wait 60 seconds before checking (don't spam)
                for _ in range(60):
                    if not project_state["running"]:
                        return
                    time.sleep(1)

                # Detect and kill orphans in this project's directory
                orphan_count = process_manager.detect_and_kill_orphans(project_path)
                if orphan_count > 0:
                    log_text = f'[{project_id}] ⚠️  Cleaned up {orphan_count} orphaned Claude process(es)'
                    project_state['log_lines'].append(log_text)
                    if len(project_state['log_lines']) > 500:
                        project_state['log_lines'] = project_state['log_lines'][-500:]
                    socketio.emit('log_line', {'line': log_text, 'project_id': project_id})
            except Exception as e:
                logger.error(f"Error in orphan cleanup thread: {e}")
    
    # Start orphan cleanup thread alongside the main orchestra thread
    cleanup_thread = threading.Thread(target=cleanup_orphans)
    cleanup_thread.daemon = True  # Dies when main thread exits
    cleanup_thread.start()

    thread = threading.Thread(target=run_orchestra)
    thread.daemon = True
    thread.start()

    pr_thread = threading.Thread(target=check_prs, args=(project_path, socketio))
    pr_thread.daemon = True
    pr_thread.start()

@socketio.on('stop_orchestra')
def handle_stop():
    global orchestra_state
    orchestra_state["running"] = False

    # Try to stop via process manager if we have a project_id
    project_id = orchestra_state.get("project_id")
    if project_id:
        process_manager.stop_process(project_id, timeout=10)
    elif orchestra_state.get("process"):
        # Fallback for legacy single-project mode
        orchestra_state["process"].terminate()

    emit('state_update', get_serializable_state())
    emit('log_line', {'line': 'Stopping orchestra...'})

if __name__ == '__main__':
    print("=" * 50)
    print("Claude Orchestra Dashboard")
    print("=" * 50)
    print("")
    print("Open http://localhost:5050 in your browser")
    print("")
    socketio.run(app, host='0.0.0.0', port=5050, debug=False, allow_unsafe_werkzeug=True)
