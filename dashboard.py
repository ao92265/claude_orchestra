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
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template_string, jsonify, request
from flask_socketio import SocketIO, emit

app = Flask(__name__)
app.config['SECRET_KEY'] = 'claude-orchestra-secret'
socketio = SocketIO(app, cors_allowed_origins="*")

# Default state template for a project
def create_project_state():
    return {
        "running": False,
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
            height: 500px;
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
            padding: 15px;
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 12px;
            line-height: 1.6;
        }
        .log-line { margin-bottom: 4px; }
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
            <input type="text" id="projectPath" placeholder="Project path (e.g., /Users/you/project)" value="">
            <button class="browse-btn" onclick="openBrowser()" title="Browse for folder">📁</button>
            <select id="recentProjects" onchange="selectRecentProject()" title="Recent projects">
                <option value="">Recent...</option>
            </select>
            <select id="maxHours">
                <option value="0">Indefinite</option>
                <option value="0.5">30 minutes</option>
                <option value="1" selected>1 hour</option>
                <option value="2">2 hours</option>
                <option value="4">4 hours</option>
                <option value="8">8 hours</option>
                <option value="24">24 hours</option>
            </select>
            <select id="taskMode">
                <option value="small">Small Tasks</option>
                <option value="normal" selected>Normal</option>
                <option value="large">Large Features</option>
            </select>
            <select id="modelSelect">
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
            <textarea id="initialGuidance" placeholder="Optional: Initial guidance for the orchestra (e.g., 'Focus on backend API tasks first' or 'Start with the authentication module')"></textarea>
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

        socket.on('connect', function() {
            console.log('Connected to server');
            socket.emit('get_state');
            socket.emit('get_all_projects');
            loadRecentProjects();
        });

        socket.on('projects_update', function(data) {
            projectsData = {};
            (data.projects || []).forEach(function(p) {
                projectsData[p.id] = p;
            });
            updateProjectTabs();
        });

        function updateProjectTabs() {
            console.log('updateProjectTabs called, projectsData:', projectsData);
            var tabs = document.getElementById('projectTabs');
            if (!tabs) {
                console.error('projectTabs element not found!');
                return;
            }
            tabs.innerHTML = '';

            // Add tabs for each project
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

            // Add "Add Project" tab at the end
            var addTab = document.createElement('div');
            addTab.className = 'project-tab' + (currentProjectId === 'new' ? ' active' : '');
            addTab.setAttribute('data-project', 'new');
            addTab.onclick = function() { console.log('Add tab clicked'); selectProject('new'); };
            addTab.innerHTML = '<span class="tab-icon">➕</span><span class="tab-name">Add Project</span>';
            tabs.appendChild(addTab);
            console.log('Tabs updated, children count:', tabs.children.length);
        }

        function selectProject(projectId) {
            console.log('selectProject called with:', projectId);
            currentProjectId = projectId;
            updateProjectTabs();

            if (projectId === 'new') {
                // Reset form for new project
                console.log('Resetting form for new project');
                document.getElementById('projectPath').value = '';
                document.getElementById('startBtn').disabled = false;
                document.getElementById('stopBtn').disabled = true;
                document.getElementById('statusBadge').textContent = 'New Project';
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
                // Focus the project path input and open browser
                document.getElementById('projectPath').focus();
                openBrowser();
            } else {
                // Load existing project state
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
                selectProject('new');
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
            updateUI(state);
        });

        socket.on('log_line', function(data) {
            addLogLine(data.line);
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
            div.className = 'log-line';

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
            logContent.scrollTop = logContent.scrollHeight;
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
        }

        function stopOrchestra() {
            if (currentProjectId && currentProjectId !== 'new') {
                socket.emit('stop_project', { project_id: currentProjectId });
            } else {
                socket.emit('stop_orchestra');
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

                    // Add directories
                    (data.dirs || []).forEach(function(dir) {
                        var div = document.createElement('div');
                        div.className = 'dir-item';
                        div.innerHTML = '<span class="dir-icon">📁</span><span class="dir-name">' + dir + '</span>';
                        div.ondblclick = function() {
                            navigateTo(path + (path.endsWith('/') ? '' : '/') + dir);
                        };
                        div.onclick = function() {
                            document.querySelectorAll('.dir-item').forEach(el => el.classList.remove('selected'));
                            div.classList.add('selected');
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
            closeBrowser();
            loadTodos();
        }
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
        if state.get("process"):
            state["process"].terminate()
        emit('state_update', get_serializable_state(state))
        emit('projects_update', {'projects': get_all_projects_summary()})
        emit('log_line', {'line': f'Stopping orchestra for {project_id}...'})

@socketio.on('remove_project')
def handle_remove_project(data):
    project_id = data.get('project_id')
    if project_id and project_id in projects_state:
        state = projects_state[project_id]
        if state.get("running") and state.get("process"):
            state["process"].terminate()
        del projects_state[project_id]
        emit('projects_update', {'projects': get_all_projects_summary()})

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
                            socketio.emit('log_line', {'line': f'[{pid}] ' + line.strip()})
                break

            # Non-blocking check if data is available (0.5s timeout)
            ready, _, _ = select.select([state["process"].stdout], [], [], 0.5)
            if not ready:
                continue  # No data, loop again to check running flag

            line = state["process"].stdout.readline()
            if line:
                line_text = line.strip()
                socketio.emit('log_line', {'line': f'[{pid}] ' + line_text})

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

                # Parse activity from stream-json events and log output
                try:
                    if line_text.startswith('{') and '"type"' in line_text:
                        event = json.loads(line_text)

                        # Track tool usage from tool_use events
                        if event.get('type') == 'tool_use':
                            tool_name = event.get('name', event.get('tool', 'unknown'))
                            state["tools_used"] += 1
                            state["last_tool"] = tool_name

                            # Track file changes
                            if tool_name in ('Edit', 'Write', 'NotebookEdit'):
                                file_path = event.get('input', {}).get('file_path', '')
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
        socketio.emit('state_update', get_serializable_state(state))
        socketio.emit('projects_update', {'projects': get_all_projects_summary()})
        socketio.emit('log_line', {'line': f'[{pid}] Orchestra stopped'})

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
    if orchestra_state.get("process"):
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
