#!/usr/bin/env python3
"""
Dashboard Claims Extension - UI and Socket handlers for multi-user task claims

This module provides the UI components and socket handlers for displaying
and managing task claims in the Claude Orchestra dashboard.

Integration:
    In dashboard.py, add these lines:

    from dashboard_claims import (
        register_claims_handlers,
        CLAIMS_CSS,
        CLAIMS_HTML,
        CLAIMS_JS
    )

    # After socketio is created:
    register_claims_handlers(socketio, app)

    # In HTML_TEMPLATE, add:
    # - CLAIMS_CSS in <style>
    # - CLAIMS_HTML in sidebar
    # - CLAIMS_JS in <script>
"""

import asyncio
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any
from flask import Flask
from flask_socketio import SocketIO, emit

logger = logging.getLogger(__name__)

# Global reference to TaskCoordinator (set by register_claims_handlers)
_coordinator = None
_config = None


def register_claims_handlers(
    socketio: SocketIO,
    app: Flask,
    coordinator=None,
    config=None
):
    """
    Register socket handlers for claims management.

    Args:
        socketio: Flask-SocketIO instance
        app: Flask app instance
        coordinator: Optional TaskCoordinator instance
        config: Optional MultiUserConfig instance
    """
    global _coordinator, _config
    _coordinator = coordinator
    _config = config

    @socketio.on('get_claims')
    def handle_get_claims():
        """Get all active task claims."""
        claims_data = get_claims_data()
        emit('claims_update', claims_data)

    @socketio.on('get_available_tasks')
    def handle_get_available_tasks(data=None):
        """Get available tasks for claiming."""
        priority = (data or {}).get('priority')
        size = (data or {}).get('size')
        tasks_data = get_available_tasks_data(priority, size)
        emit('available_tasks_update', tasks_data)

    @socketio.on('release_claim')
    def handle_release_claim(data):
        """Release a claim on a task."""
        issue_number = data.get('issue_number')
        reason = data.get('reason', 'manual_release')

        if not issue_number:
            emit('claims_error', {'error': 'Issue number required'})
            return

        result = release_claim_sync(issue_number, reason)
        if result['success']:
            emit('claim_released', {'issue_number': issue_number})
            # Broadcast update to all clients
            socketio.emit('claims_update', get_claims_data())
        else:
            emit('claims_error', result)

    @socketio.on('reclaim_stale')
    def handle_reclaim_stale():
        """Release all stale claims."""
        result = reclaim_stale_sync()
        emit('stale_reclaimed', result)
        socketio.emit('claims_update', get_claims_data())

    logger.info("Claims socket handlers registered")


def get_claims_data() -> Dict[str, Any]:
    """Get current claims data for the UI."""
    if not _coordinator:
        return {
            'enabled': False,
            'claims': [],
            'my_agent_id': None,
            'error': 'Multi-user mode not enabled'
        }

    try:
        # Run async function in sync context
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            claims = loop.run_until_complete(_coordinator.get_all_active_claims())
            my_claims = loop.run_until_complete(_coordinator.get_my_claimed_tasks())
        finally:
            loop.close()

        return {
            'enabled': True,
            'claims': [
                {
                    'issue_number': c.issue_number,
                    'agent_id': c.agent_id,
                    'github_username': c.github_username,
                    'claimed_at': c.claimed_at,
                    'last_heartbeat': c.last_heartbeat,
                    'branch_name': c.branch_name,
                    'is_mine': c.agent_id == _coordinator.agent.agent_id if _coordinator.agent else False,
                    'age_minutes': _calculate_age_minutes(c.last_heartbeat)
                }
                for c in claims
            ],
            'my_agent_id': _coordinator.agent.agent_id if _coordinator.agent else None,
            'my_claims_count': len(my_claims)
        }
    except Exception as e:
        logger.error(f"Error getting claims data: {e}")
        return {
            'enabled': True,
            'claims': [],
            'error': str(e)
        }


def get_available_tasks_data(priority: Optional[str] = None, size: Optional[str] = None) -> Dict:
    """Get available tasks data for the UI."""
    if not _coordinator:
        return {'enabled': False, 'tasks': []}

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            tasks = loop.run_until_complete(
                _coordinator.get_available_tasks(priority=priority, size=size, limit=20)
            )
        finally:
            loop.close()

        return {
            'enabled': True,
            'tasks': [
                {
                    'issue_number': t.issue_number,
                    'title': t.title,
                    'priority': t.priority.value if t.priority else None,
                    'size': t.size.value if t.size else None,
                    'created_at': t.created_at
                }
                for t in tasks
            ]
        }
    except Exception as e:
        logger.error(f"Error getting available tasks: {e}")
        return {'enabled': True, 'tasks': [], 'error': str(e)}


def release_claim_sync(issue_number: int, reason: str) -> Dict:
    """Release a claim synchronously."""
    if not _coordinator:
        return {'success': False, 'error': 'Coordinator not available'}

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_coordinator.release_claim(issue_number, reason))
        finally:
            loop.close()
        return {'success': True}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def reclaim_stale_sync() -> Dict:
    """Reclaim stale tasks synchronously."""
    if not _coordinator:
        return {'success': False, 'error': 'Coordinator not available'}

    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            count = loop.run_until_complete(_coordinator.reclaim_stale_tasks())
        finally:
            loop.close()
        return {'success': True, 'released_count': count}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def _calculate_age_minutes(timestamp_str: str) -> int:
    """Calculate age in minutes from ISO timestamp."""
    try:
        ts = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
        age = datetime.now(timezone.utc) - ts
        return int(age.total_seconds() / 60)
    except:
        return 0


# =============================================================================
# CSS for Claims UI
# =============================================================================

CLAIMS_CSS = """
/* Claims Panel Styles */
.claims-panel {
    margin-top: 20px;
}

.claims-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 15px;
}

.claims-header h3 {
    font-size: 14px;
    color: #8b949e;
    text-transform: uppercase;
}

.claims-badge {
    background: #238636;
    color: white;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 600;
}

.claims-list {
    list-style: none;
}

.claim-item {
    padding: 12px;
    background: #21262d;
    border-radius: 8px;
    margin-bottom: 10px;
    border-left: 3px solid #30363d;
}

.claim-item.mine {
    border-left-color: #238636;
}

.claim-item.stale {
    border-left-color: #d29922;
    opacity: 0.8;
}

.claim-issue {
    font-weight: 600;
    color: #58a6ff;
    margin-bottom: 5px;
}

.claim-agent {
    font-size: 11px;
    color: #8b949e;
    margin-bottom: 5px;
    font-family: 'Monaco', 'Menlo', monospace;
}

.claim-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 8px;
}

.claim-heartbeat {
    font-size: 11px;
    display: flex;
    align-items: center;
    gap: 5px;
}

.claim-heartbeat.fresh {
    color: #238636;
}

.claim-heartbeat.warning {
    color: #d29922;
}

.claim-heartbeat.stale {
    color: #f85149;
}

.heartbeat-dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    display: inline-block;
}

.heartbeat-dot.fresh {
    background: #238636;
    animation: pulse-green 2s infinite;
}

.heartbeat-dot.warning {
    background: #d29922;
}

.heartbeat-dot.stale {
    background: #f85149;
}

@keyframes pulse-green {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.5; }
}

.claim-release-btn {
    padding: 4px 8px;
    font-size: 10px;
    background: #21262d;
    border: 1px solid #f85149;
    color: #f85149;
    border-radius: 4px;
    cursor: pointer;
}

.claim-release-btn:hover {
    background: #f8514920;
}

.claims-actions {
    margin-top: 15px;
    display: flex;
    gap: 10px;
}

.claims-actions button {
    flex: 1;
    padding: 8px;
    font-size: 12px;
}

.available-tasks {
    margin-top: 20px;
}

.available-tasks h4 {
    font-size: 12px;
    color: #8b949e;
    margin-bottom: 10px;
}

.task-item {
    padding: 10px;
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 6px;
    margin-bottom: 8px;
    display: flex;
    align-items: center;
    gap: 10px;
}

.task-number {
    background: #30363d;
    color: #8b949e;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 11px;
}

.task-title {
    flex: 1;
    font-size: 12px;
    color: #c9d1d9;
}

.task-labels {
    display: flex;
    gap: 5px;
}

.task-label {
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 500;
}

.task-label.priority-high { background: #b6020520; color: #f85149; border: 1px solid #b60205; }
.task-label.priority-medium { background: #d2992220; color: #d29922; border: 1px solid #d29922; }
.task-label.priority-low { background: #23863620; color: #238636; border: 1px solid #238636; }
.task-label.size-small { background: #c2e0c620; color: #7ee787; }
.task-label.size-medium { background: #fef2c020; color: #d29922; }
.task-label.size-large { background: #f9d0c420; color: #f85149; }

.multi-user-disabled {
    padding: 20px;
    text-align: center;
    color: #8b949e;
    font-size: 12px;
}

.multi-user-disabled code {
    display: block;
    margin-top: 10px;
    background: #21262d;
    padding: 10px;
    border-radius: 6px;
    font-size: 11px;
}
"""


# =============================================================================
# HTML for Claims UI
# =============================================================================

CLAIMS_HTML = """
<div class="card claims-panel" id="claims-panel">
    <div class="claims-header">
        <h3>Task Claims</h3>
        <span class="claims-badge" id="claims-count">0</span>
    </div>

    <div id="claims-content">
        <div class="multi-user-disabled" id="multi-user-disabled" style="display: none;">
            <p>Multi-user mode is not enabled.</p>
            <code>export ORCHESTRA_MULTI_USER=true</code>
        </div>

        <ul class="claims-list" id="claims-list"></ul>

        <div class="claims-actions">
            <button onclick="refreshClaims()" title="Refresh claims">
                ↻ Refresh
            </button>
            <button onclick="reclaimStale()" class="danger" title="Release stale claims">
                🧹 Reclaim Stale
            </button>
        </div>
    </div>

    <div class="available-tasks">
        <h4>Available Tasks (<span id="available-count">0</span>)</h4>
        <div id="available-tasks-list"></div>
    </div>
</div>
"""


# =============================================================================
# JavaScript for Claims UI
# =============================================================================

CLAIMS_JS = """
// Claims Management
let claimsData = { enabled: false, claims: [], my_agent_id: null };
let availableTasksData = { tasks: [] };

socket.on('claims_update', function(data) {
    claimsData = data;
    renderClaims();
});

socket.on('available_tasks_update', function(data) {
    availableTasksData = data;
    renderAvailableTasks();
});

socket.on('claim_released', function(data) {
    console.log('Claim released:', data.issue_number);
    refreshClaims();
});

socket.on('stale_reclaimed', function(data) {
    if (data.success) {
        console.log('Reclaimed', data.released_count, 'stale claims');
    }
});

socket.on('claims_error', function(data) {
    console.error('Claims error:', data.error);
});

function refreshClaims() {
    socket.emit('get_claims');
    socket.emit('get_available_tasks');
}

function reclaimStale() {
    if (confirm('Release all stale claims (tasks with no heartbeat for 30+ min)?')) {
        socket.emit('reclaim_stale');
    }
}

function releaseClaim(issueNumber) {
    if (confirm('Release claim on issue #' + issueNumber + '?')) {
        socket.emit('release_claim', { issue_number: issueNumber, reason: 'manual_release' });
    }
}

function renderClaims() {
    const container = document.getElementById('claims-list');
    const countBadge = document.getElementById('claims-count');
    const disabledMsg = document.getElementById('multi-user-disabled');

    if (!claimsData.enabled) {
        disabledMsg.style.display = 'block';
        container.innerHTML = '';
        countBadge.textContent = '-';
        return;
    }

    disabledMsg.style.display = 'none';
    countBadge.textContent = claimsData.claims.length;

    if (claimsData.claims.length === 0) {
        container.innerHTML = '<li style="color: #8b949e; font-size: 12px; padding: 10px;">No active claims</li>';
        return;
    }

    container.innerHTML = claimsData.claims.map(claim => {
        const isMine = claim.is_mine;
        const ageMinutes = claim.age_minutes || 0;
        const isStale = ageMinutes > 30;
        const isWarning = ageMinutes > 15;

        const heartbeatClass = isStale ? 'stale' : (isWarning ? 'warning' : 'fresh');
        const heartbeatText = ageMinutes < 1 ? 'just now' : ageMinutes + ' min ago';

        return `
            <li class="claim-item ${isMine ? 'mine' : ''} ${isStale ? 'stale' : ''}">
                <div class="claim-issue">#${claim.issue_number}</div>
                <div class="claim-agent">
                    ${isMine ? '👤 ' : ''}${claim.agent_id.substring(0, 30)}...
                </div>
                <div class="claim-meta">
                    <div class="claim-heartbeat ${heartbeatClass}">
                        <span class="heartbeat-dot ${heartbeatClass}"></span>
                        ${heartbeatText}
                    </div>
                    ${isMine ? `<button class="claim-release-btn" onclick="releaseClaim(${claim.issue_number})">Release</button>` : ''}
                </div>
            </li>
        `;
    }).join('');
}

function renderAvailableTasks() {
    const container = document.getElementById('available-tasks-list');
    const countSpan = document.getElementById('available-count');

    if (!availableTasksData.tasks || availableTasksData.tasks.length === 0) {
        container.innerHTML = '<div style="color: #8b949e; font-size: 12px;">No available tasks</div>';
        countSpan.textContent = '0';
        return;
    }

    countSpan.textContent = availableTasksData.tasks.length;

    container.innerHTML = availableTasksData.tasks.slice(0, 5).map(task => {
        const priorityLabel = task.priority ?
            `<span class="task-label priority-${task.priority}">${task.priority}</span>` : '';
        const sizeLabel = task.size ?
            `<span class="task-label size-${task.size}">${task.size}</span>` : '';

        return `
            <div class="task-item">
                <span class="task-number">#${task.issue_number}</span>
                <span class="task-title">${escapeHtml(task.title.substring(0, 40))}${task.title.length > 40 ? '...' : ''}</span>
                <div class="task-labels">
                    ${priorityLabel}
                    ${sizeLabel}
                </div>
            </div>
        `;
    }).join('');
}

function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Initial load
setTimeout(refreshClaims, 1000);
// Refresh claims periodically
setInterval(refreshClaims, 30000);
"""


# =============================================================================
# Integration Instructions
# =============================================================================

INTEGRATION_INSTRUCTIONS = """
# Dashboard Claims Integration

To add the claims UI to the existing dashboard:

1. Import the claims module at the top of dashboard.py:

    from dashboard_claims import (
        register_claims_handlers,
        CLAIMS_CSS,
        CLAIMS_HTML,
        CLAIMS_JS
    )

2. After creating the socketio instance, register the handlers:

    # After: socketio = SocketIO(app, cors_allowed_origins="*")
    # Add:
    from task_coordinator import TaskCoordinator
    from multi_user_config import MultiUserConfig

    config = MultiUserConfig.from_env()
    coordinator = None

    if config.enabled and config.is_valid():
        coordinator = TaskCoordinator(
            repo_owner=config.repo_owner,
            repo_name=config.repo_name,
            github_token=config.github_token,
            claim_timeout=config.claim_timeout
        )
        # Note: coordinator.setup() needs to be called async at startup

    register_claims_handlers(socketio, app, coordinator, config)

3. Add CLAIMS_CSS to the <style> section of HTML_TEMPLATE

4. Add CLAIMS_HTML to the sidebar section of HTML_TEMPLATE
   (after the existing PR list card)

5. Add CLAIMS_JS to the <script> section of HTML_TEMPLATE
   (after the existing socket event handlers)

6. Start the dashboard with multi-user environment variables:

    export ORCHESTRA_MULTI_USER=true
    export GITHUB_TOKEN=ghp_xxx
    export GITHUB_REPO=owner/repo
    python dashboard.py
"""

if __name__ == "__main__":
    print(INTEGRATION_INSTRUCTIONS)
