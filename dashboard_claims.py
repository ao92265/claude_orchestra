#!/usr/bin/env python3
"""
Dashboard Multi-User Extension - Setup wizard and claims management UI

Provides a complete GUI for:
- Configuring multi-user mode (GitHub token, repo, timeouts)
- Syncing TODO.md to GitHub Issues
- Viewing and managing task claims
- Monitoring other agents' activity

Integration:
    In dashboard.py, add at the top:
        from dashboard_claims import register_claims_handlers, get_multiuser_html_components

    After socketio is created:
        register_claims_handlers(socketio, app)

    In HTML_TEMPLATE:
        - Add CSS from get_multiuser_html_components()['css']
        - Add HTML from get_multiuser_html_components()['html'] in sidebar
        - Add JS from get_multiuser_html_components()['js'] in script section
"""

import asyncio
import os
import logging
from datetime import datetime, timezone
from typing import Optional, Dict, List, Any
from pathlib import Path

logger = logging.getLogger(__name__)

# Global state
_coordinator = None
_config = None
_setup_state = {
    'configured': False,
    'github_token': '',
    'repo_owner': '',
    'repo_name': '',
    'claim_timeout': 1800,
    'heartbeat_interval': 300,
    'last_sync': None,
    'connection_tested': False,
    'github_username': None,
    'project_path': None  # Current project path for TODO sync
}


def register_claims_handlers(socketio, app, coordinator=None, config=None):
    """Register all socket handlers for multi-user functionality."""
    global _coordinator, _config

    _coordinator = coordinator
    _config = config

    # Load initial config from environment
    _load_config_from_env()

    # =========================================================================
    # Setup & Configuration Handlers
    # =========================================================================

    @socketio.on('get_multiuser_config')
    def handle_get_config():
        """Get current multi-user configuration."""
        from flask_socketio import emit
        emit('multiuser_config', _get_safe_config())

    @socketio.on('save_multiuser_config')
    def handle_save_config(data):
        """Save multi-user configuration."""
        from flask_socketio import emit
        global _setup_state

        _setup_state['github_token'] = data.get('github_token', '')
        repo = data.get('repo', '')
        if '/' in repo:
            _setup_state['repo_owner'], _setup_state['repo_name'] = repo.split('/', 1)
        _setup_state['claim_timeout'] = int(data.get('claim_timeout', 1800))
        _setup_state['heartbeat_interval'] = int(data.get('heartbeat_interval', 300))

        # Also set environment variables so other components can use them
        if _setup_state['github_token']:
            os.environ['GITHUB_TOKEN'] = _setup_state['github_token']
        if _setup_state['repo_owner'] and _setup_state['repo_name']:
            os.environ['GITHUB_REPO'] = f"{_setup_state['repo_owner']}/{_setup_state['repo_name']}"
        os.environ['ORCHESTRA_MULTI_USER'] = 'true'
        os.environ['ORCHESTRA_CLAIM_TIMEOUT'] = str(_setup_state['claim_timeout'])
        os.environ['ORCHESTRA_HEARTBEAT_INTERVAL'] = str(_setup_state['heartbeat_interval'])

        _setup_state['configured'] = bool(
            _setup_state['github_token'] and
            _setup_state['repo_owner'] and
            _setup_state['repo_name']
        )

        emit('multiuser_config', _get_safe_config())
        emit('config_saved', {'success': True})
        logger.info(f"Multi-user config saved for {_setup_state['repo_owner']}/{_setup_state['repo_name']}")

    @socketio.on('test_github_connection')
    def handle_test_connection():
        """Test GitHub API connection with provided token."""
        from flask_socketio import emit
        global _setup_state

        if not _setup_state['github_token']:
            emit('connection_result', {'success': False, 'error': 'No GitHub token configured'})
            return

        try:
            result = _test_github_connection_sync()
            if result['success']:
                _setup_state['connection_tested'] = True
                _setup_state['github_username'] = result.get('username')
            emit('connection_result', result)
        except Exception as e:
            emit('connection_result', {'success': False, 'error': str(e)})

    @socketio.on('sync_todos')
    def handle_sync_todos(data=None):
        """Sync TODO.md to GitHub Issues."""
        from flask_socketio import emit
        global _setup_state

        if not _setup_state['configured']:
            emit('sync_result', {'success': False, 'error': 'Multi-user mode not configured'})
            return

        # Get project path from data or use stored path
        project_path = (data or {}).get('project_path') or _setup_state.get('project_path')
        if not project_path:
            emit('sync_result', {'success': False, 'error': 'No project path specified'})
            return

        # Store for future use
        _setup_state['project_path'] = project_path

        try:
            result = _sync_todos_sync(project_path)
            if result['success']:
                _setup_state['last_sync'] = datetime.now(timezone.utc).isoformat()
            emit('sync_result', result)
            emit('multiuser_config', _get_safe_config())
        except Exception as e:
            emit('sync_result', {'success': False, 'error': str(e)})

    # =========================================================================
    # Claims Management Handlers
    # =========================================================================

    @socketio.on('get_claims')
    def handle_get_claims():
        """Get all active task claims."""
        from flask_socketio import emit
        claims_data = _get_claims_data_sync()
        emit('claims_update', claims_data)

    @socketio.on('get_available_tasks')
    def handle_get_available_tasks(data=None):
        """Get available tasks for claiming."""
        from flask_socketio import emit
        priority = (data or {}).get('priority')
        size = (data or {}).get('size')
        tasks_data = _get_available_tasks_sync(priority, size)
        emit('available_tasks_update', tasks_data)

    @socketio.on('release_claim')
    def handle_release_claim(data):
        """Release a claim on a task."""
        from flask_socketio import emit
        issue_number = data.get('issue_number')
        reason = data.get('reason', 'manual_release')

        if not issue_number:
            emit('claims_error', {'error': 'Issue number required'})
            return

        result = _release_claim_sync(issue_number, reason)
        if result['success']:
            emit('claim_released', {'issue_number': issue_number})
            socketio.emit('claims_update', _get_claims_data_sync())
        else:
            emit('claims_error', result)

    @socketio.on('reclaim_stale')
    def handle_reclaim_stale():
        """Release all stale claims."""
        from flask_socketio import emit
        result = _reclaim_stale_sync()
        emit('stale_reclaimed', result)
        socketio.emit('claims_update', _get_claims_data_sync())

    @socketio.on('get_repo_from_project')
    def handle_get_repo_from_project(data):
        """Get GitHub repository from a project's git remote."""
        from flask_socketio import emit
        project_path = data.get('project_path', '')

        if not project_path or not os.path.isdir(project_path):
            emit('project_repo', {'success': False, 'error': 'Invalid project path'})
            return

        repo = _get_repo_from_git_remote(project_path)
        if repo:
            emit('project_repo', {'success': True, 'repo': repo})
        else:
            emit('project_repo', {'success': False, 'error': 'Could not detect GitHub repo from git remote'})

    logger.info("Multi-user socket handlers registered")


# =============================================================================
# Helper Functions
# =============================================================================

def _get_repo_from_git_remote(project_path: str) -> Optional[str]:
    """Extract owner/repo from a project's git remote URL."""
    import subprocess
    import re

    try:
        # Get the origin remote URL
        result = subprocess.run(
            ['git', 'remote', 'get-url', 'origin'],
            cwd=project_path,
            capture_output=True,
            text=True,
            timeout=5
        )

        if result.returncode != 0:
            return None

        remote_url = result.stdout.strip()

        # Parse GitHub URL formats:
        # https://github.com/owner/repo.git
        # https://github.com/owner/repo
        # git@github.com:owner/repo.git
        # git@github.com:owner/repo

        # HTTPS format
        https_match = re.search(r'github\.com[/:]([^/]+)/([^/\s]+?)(?:\.git)?$', remote_url)
        if https_match:
            owner, repo = https_match.groups()
            return f"{owner}/{repo}"

        # SSH format
        ssh_match = re.search(r'git@github\.com:([^/]+)/([^/\s]+?)(?:\.git)?$', remote_url)
        if ssh_match:
            owner, repo = ssh_match.groups()
            return f"{owner}/{repo}"

        return None

    except Exception as e:
        logger.error(f"Error getting git remote: {e}")
        return None


def _load_config_from_env():
    """Load configuration from environment variables."""
    global _setup_state

    _setup_state['github_token'] = os.getenv('GITHUB_TOKEN', '')

    repo = os.getenv('GITHUB_REPO', '')
    if '/' in repo:
        _setup_state['repo_owner'], _setup_state['repo_name'] = repo.split('/', 1)
    else:
        _setup_state['repo_owner'] = os.getenv('GITHUB_REPO_OWNER', '')
        _setup_state['repo_name'] = os.getenv('GITHUB_REPO_NAME', '')

    _setup_state['claim_timeout'] = int(os.getenv('ORCHESTRA_CLAIM_TIMEOUT', '1800'))
    _setup_state['heartbeat_interval'] = int(os.getenv('ORCHESTRA_HEARTBEAT_INTERVAL', '300'))

    _setup_state['configured'] = bool(
        _setup_state['github_token'] and
        _setup_state['repo_owner'] and
        _setup_state['repo_name']
    )


def _get_safe_config() -> Dict:
    """Get configuration without exposing full token."""
    token = _setup_state['github_token']
    masked_token = f"{'*' * 8}...{token[-4:]}" if len(token) > 4 else ''

    return {
        'configured': _setup_state['configured'],
        'has_token': bool(token),
        'masked_token': masked_token,
        'repo': f"{_setup_state['repo_owner']}/{_setup_state['repo_name']}" if _setup_state['repo_owner'] else '',
        'claim_timeout': _setup_state['claim_timeout'],
        'heartbeat_interval': _setup_state['heartbeat_interval'],
        'last_sync': _setup_state['last_sync'],
        'connection_tested': _setup_state['connection_tested'],
        'github_username': _setup_state['github_username']
    }


def _test_github_connection_sync() -> Dict:
    """Test GitHub connection synchronously."""
    import aiohttp

    async def test():
        async with aiohttp.ClientSession() as session:
            headers = {
                'Authorization': f"token {_setup_state['github_token']}",
                'Accept': 'application/vnd.github.v3+json'
            }
            async with session.get('https://api.github.com/user', headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return {'success': True, 'username': data['login']}
                elif resp.status == 401:
                    return {'success': False, 'error': 'Invalid token - authentication failed'}
                else:
                    return {'success': False, 'error': f'GitHub API error: {resp.status}'}

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(test())
    finally:
        loop.close()


def _sync_todos_sync(project_path: str) -> Dict:
    """Sync TODOs synchronously."""
    try:
        from task_coordinator import TaskCoordinator

        async def sync():
            coordinator = TaskCoordinator(
                repo_owner=_setup_state['repo_owner'],
                repo_name=_setup_state['repo_name'],
                github_token=_setup_state['github_token'],
                project_path=project_path,  # Pass project path for TODO.md location
                claim_timeout=_setup_state['claim_timeout']
            )
            await coordinator.setup()
            result = await coordinator.sync_todos_to_issues()
            await coordinator.close()
            return {
                'success': True,
                'created': result.created,
                'updated': result.updated,
                'unchanged': result.unchanged,
                'errors': result.errors
            }

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(sync())
        finally:
            loop.close()

    except ImportError:
        return {'success': False, 'error': 'task_coordinator module not found'}
    except Exception as e:
        return {'success': False, 'error': str(e)}


def _get_claims_data_sync() -> Dict:
    """Get claims data synchronously."""
    if not _setup_state['configured']:
        return {'enabled': False, 'claims': [], 'error': 'Not configured'}

    try:
        from task_coordinator import TaskCoordinator

        async def get_claims():
            coordinator = TaskCoordinator(
                repo_owner=_setup_state['repo_owner'],
                repo_name=_setup_state['repo_name'],
                github_token=_setup_state['github_token'],
                claim_timeout=_setup_state['claim_timeout']
            )
            await coordinator.setup()
            claims = await coordinator.get_all_active_claims()
            agent_id = coordinator.agent.agent_id if coordinator.agent else None
            await coordinator.close()

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
                        'is_mine': c.agent_id == agent_id if agent_id else False,
                        'age_minutes': _calculate_age_minutes(c.last_heartbeat)
                    }
                    for c in claims
                ],
                'my_agent_id': agent_id
            }

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(get_claims())
        finally:
            loop.close()

    except Exception as e:
        return {'enabled': False, 'claims': [], 'error': str(e)}


def _get_available_tasks_sync(priority: str = None, size: str = None) -> Dict:
    """Get available tasks synchronously."""
    if not _setup_state['configured']:
        return {'enabled': False, 'tasks': []}

    try:
        from task_coordinator import TaskCoordinator

        async def get_tasks():
            coordinator = TaskCoordinator(
                repo_owner=_setup_state['repo_owner'],
                repo_name=_setup_state['repo_name'],
                github_token=_setup_state['github_token'],
                claim_timeout=_setup_state['claim_timeout']
            )
            await coordinator.setup()
            tasks = await coordinator.get_available_tasks(priority=priority, size=size, limit=20)
            await coordinator.close()

            return {
                'enabled': True,
                'tasks': [
                    {
                        'issue_number': t.issue_number,
                        'title': t.title,
                        'priority': t.priority.value if t.priority else None,
                        'size': t.size.value if t.size else None
                    }
                    for t in tasks
                ]
            }

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(get_tasks())
        finally:
            loop.close()

    except Exception as e:
        return {'enabled': False, 'tasks': [], 'error': str(e)}


def _release_claim_sync(issue_number: int, reason: str) -> Dict:
    """Release claim synchronously."""
    if not _setup_state['configured']:
        return {'success': False, 'error': 'Not configured'}

    try:
        from task_coordinator import TaskCoordinator

        async def release():
            coordinator = TaskCoordinator(
                repo_owner=_setup_state['repo_owner'],
                repo_name=_setup_state['repo_name'],
                github_token=_setup_state['github_token'],
                claim_timeout=_setup_state['claim_timeout']
            )
            await coordinator.setup()
            await coordinator.release_claim(issue_number, reason)
            await coordinator.close()
            return {'success': True}

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(release())
        finally:
            loop.close()

    except Exception as e:
        return {'success': False, 'error': str(e)}


def _reclaim_stale_sync() -> Dict:
    """Reclaim stale tasks synchronously."""
    if not _setup_state['configured']:
        return {'success': False, 'error': 'Not configured'}

    try:
        from task_coordinator import TaskCoordinator

        async def reclaim():
            coordinator = TaskCoordinator(
                repo_owner=_setup_state['repo_owner'],
                repo_name=_setup_state['repo_name'],
                github_token=_setup_state['github_token'],
                claim_timeout=_setup_state['claim_timeout']
            )
            await coordinator.setup()
            count = await coordinator.reclaim_stale_tasks()
            await coordinator.close()
            return {'success': True, 'released_count': count}

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(reclaim())
        finally:
            loop.close()

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
# HTML Components
# =============================================================================

def get_multiuser_html_components() -> Dict[str, str]:
    """Get all HTML components for multi-user UI."""
    return {
        'css': MULTIUSER_CSS,
        'html': MULTIUSER_HTML,
        'js': MULTIUSER_JS
    }


MULTIUSER_CSS = """
/* Multi-User Setup Panel */
.multiuser-panel {
    margin-top: 20px;
}

.multiuser-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 15px;
    cursor: pointer;
}

.multiuser-header h3 {
    font-size: 14px;
    color: #8b949e;
    text-transform: uppercase;
    display: flex;
    align-items: center;
    gap: 8px;
}

.multiuser-status {
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
}

.multiuser-status.configured { background: #238636; color: white; }
.multiuser-status.not-configured { background: #6e7681; color: white; }

.multiuser-content {
    display: none;
}

.multiuser-content.expanded {
    display: block;
}

/* Setup Form */
.setup-form {
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 8px;
    padding: 15px;
    margin-bottom: 15px;
}

.setup-form h4 {
    font-size: 13px;
    color: #c9d1d9;
    margin-bottom: 12px;
    display: flex;
    align-items: center;
    gap: 8px;
}

.form-group {
    margin-bottom: 12px;
}

.form-group label {
    display: block;
    font-size: 11px;
    color: #8b949e;
    margin-bottom: 4px;
    text-transform: uppercase;
}

.form-group input {
    width: 100%;
    padding: 8px 10px;
    background: #21262d;
    border: 1px solid #30363d;
    border-radius: 6px;
    color: #c9d1d9;
    font-size: 13px;
}

.form-group input:focus {
    outline: none;
    border-color: #58a6ff;
}

.form-group input.success {
    border-color: #238636;
}

.form-group input.error {
    border-color: #f85149;
}

.form-group small {
    display: block;
    margin-top: 4px;
    font-size: 10px;
    color: #6e7681;
}

.form-row {
    display: flex;
    gap: 10px;
}

.form-row .form-group {
    flex: 1;
}

.form-actions {
    display: flex;
    gap: 8px;
    margin-top: 15px;
}

.form-actions button {
    flex: 1;
    padding: 8px 12px;
    border-radius: 6px;
    font-size: 12px;
    font-weight: 600;
    cursor: pointer;
    border: 1px solid #30363d;
    background: #21262d;
    color: #c9d1d9;
    transition: all 0.2s;
}

.form-actions button:hover {
    background: #30363d;
}

.form-actions button.primary {
    background: #238636;
    border-color: #238636;
    color: white;
}

.form-actions button.primary:hover {
    background: #2ea043;
}

.form-actions button:disabled {
    opacity: 0.5;
    cursor: not-allowed;
}

/* Connection Status */
.connection-status {
    padding: 10px;
    border-radius: 6px;
    margin-top: 10px;
    font-size: 12px;
    display: none;
}

.connection-status.success {
    display: block;
    background: #23863620;
    border: 1px solid #238636;
    color: #238636;
}

.connection-status.error {
    display: block;
    background: #f8514920;
    border: 1px solid #f85149;
    color: #f85149;
}

/* Sync Results */
.sync-results {
    padding: 10px;
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 6px;
    margin-top: 10px;
    font-size: 12px;
}

.sync-results .stat {
    display: flex;
    justify-content: space-between;
    padding: 4px 0;
}

.sync-results .stat-value {
    font-weight: 600;
    color: #58a6ff;
}

/* Claims Section */
.claims-section {
    margin-top: 15px;
}

.claims-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 10px;
}

.claims-header h4 {
    font-size: 12px;
    color: #8b949e;
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
    max-height: 200px;
    overflow-y: auto;
}

.claim-item {
    padding: 10px;
    background: #21262d;
    border-radius: 6px;
    margin-bottom: 8px;
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
    font-size: 13px;
}

.claim-agent {
    font-size: 10px;
    color: #8b949e;
    font-family: monospace;
    margin-top: 4px;
}

.claim-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-top: 6px;
}

.heartbeat-indicator {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 10px;
}

.heartbeat-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
}

.heartbeat-dot.fresh { background: #238636; }
.heartbeat-dot.warning { background: #d29922; }
.heartbeat-dot.stale { background: #f85149; }

.claim-release-btn {
    padding: 3px 6px;
    font-size: 9px;
    background: transparent;
    border: 1px solid #f85149;
    color: #f85149;
    border-radius: 4px;
    cursor: pointer;
}

.claim-release-btn:hover {
    background: #f8514920;
}

/* Available Tasks */
.available-section {
    margin-top: 15px;
}

.available-section h4 {
    font-size: 12px;
    color: #8b949e;
    margin-bottom: 10px;
}

.task-item {
    padding: 8px 10px;
    background: #0d1117;
    border: 1px solid #30363d;
    border-radius: 6px;
    margin-bottom: 6px;
    display: flex;
    align-items: center;
    gap: 8px;
}

.task-number {
    background: #30363d;
    color: #8b949e;
    padding: 2px 6px;
    border-radius: 4px;
    font-size: 10px;
    font-weight: 600;
}

.task-title {
    flex: 1;
    font-size: 11px;
    color: #c9d1d9;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.task-labels {
    display: flex;
    gap: 4px;
}

.task-label {
    padding: 2px 5px;
    border-radius: 3px;
    font-size: 9px;
    font-weight: 500;
}

.task-label.high { background: #b6020530; color: #f85149; }
.task-label.medium { background: #d2992230; color: #d29922; }
.task-label.low { background: #23863630; color: #238636; }
.task-label.small { background: #23863620; color: #7ee787; }
.task-label.large { background: #f8514920; color: #ffa198; }

/* Refresh Actions */
.refresh-actions {
    display: flex;
    gap: 8px;
    margin-top: 10px;
}

.refresh-actions button {
    flex: 1;
    padding: 6px 10px;
    font-size: 11px;
    background: #21262d;
    border: 1px solid #30363d;
    color: #8b949e;
    border-radius: 4px;
    cursor: pointer;
}

.refresh-actions button:hover {
    background: #30363d;
    color: #c9d1d9;
}

/* Loading spinner */
.spinner {
    display: inline-block;
    width: 12px;
    height: 12px;
    border: 2px solid #30363d;
    border-top-color: #58a6ff;
    border-radius: 50%;
    animation: spin 1s linear infinite;
}

@keyframes spin {
    to { transform: rotate(360deg); }
}

.hidden { display: none !important; }
"""


MULTIUSER_HTML = """
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
"""


MULTIUSER_JS = """
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
    }
}

// Socket handlers
socket.on('multiuser_config', function(data) {
    multiuserConfig = data;
    updateConfigUI();
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

// UI Functions
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
    socket.emit('sync_todos');
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
    if (document.getElementById('multiuser-content').classList.contains('expanded')) {
        refreshClaims();
    }
}, 30000);
"""
