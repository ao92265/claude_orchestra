#!/usr/bin/env python3
"""
Claude Orchestra Dashboard - Web GUI for monitoring multi-agent pipeline
"""

import os
import json
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

# Global state
orchestra_state = {
    "running": False,
    "project_path": None,
    "current_cycle": 0,
    "current_stage": None,
    "start_time": None,
    "max_hours": None,
    "cycles_completed": 0,
    "prs_created": [],
    "log_lines": [],
    "process": None
}

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
        }
        .stage-item.active {
            border-color: #58a6ff;
            background: #1f6feb20;
        }
        .stage-item.completed {
            border-color: #238636;
            background: #23863620;
        }
        .stage-icon { font-size: 20px; margin-bottom: 5px; }
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
    </style>
</head>
<body>
    <div class="header">
        <h1>Claude Orchestra Dashboard</h1>
        <span class="status-badge" id="statusBadge">Stopped</span>
    </div>

    <div class="container">
        <div class="controls">
            <input type="text" id="projectPath" placeholder="Project path (e.g., /Users/you/project)" value="">
            <select id="maxHours">
                <option value="0.5">30 minutes</option>
                <option value="1" selected>1 hour</option>
                <option value="2">2 hours</option>
                <option value="4">4 hours</option>
                <option value="8">8 hours</option>
            </select>
            <button class="primary" id="startBtn" onclick="startOrchestra()">Start Orchestra</button>
            <button class="danger" id="stopBtn" onclick="stopOrchestra()" disabled>Stop</button>
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

        <div class="card" style="margin-bottom: 20px;">
            <div class="card-title">Pipeline Stage</div>
            <div class="stage-pipeline">
                <div class="stage-item" id="stage-implement">
                    <div class="stage-icon">🔨</div>
                    Implementer
                </div>
                <div class="stage-item" id="stage-test">
                    <div class="stage-icon">🧪</div>
                    Tester
                </div>
                <div class="stage-item" id="stage-review">
                    <div class="stage-icon">👀</div>
                    Reviewer
                </div>
                <div class="stage-item" id="stage-plan">
                    <div class="stage-icon">📋</div>
                    Planner
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
            </div>
        </div>
    </div>

    <script>
        const socket = io();
        let startTime = null;
        let maxSeconds = null;
        let timerInterval = null;

        socket.on('connect', function() {
            console.log('Connected to server');
            socket.emit('get_state');
        });

        socket.on('state_update', function(state) {
            updateUI(state);
        });

        socket.on('log_line', function(data) {
            addLogLine(data.line);
        });

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

            // Update stage highlights
            var stages = ['implement', 'test', 'review', 'plan'];
            stages.forEach(function(s) {
                document.getElementById('stage-' + s).className = 'stage-item';
            });
            if (state.current_stage) {
                var stageEl = document.getElementById('stage-' + state.current_stage);
                if (stageEl) stageEl.classList.add('active');
            }

            if (state.running && state.start_time) {
                startTime = new Date(state.start_time);
                maxSeconds = state.max_hours * 3600;
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
            var mins = Math.floor(elapsed / 60);
            var secs = elapsed % 60;
            document.getElementById('timeElapsed').textContent =
                String(mins).padStart(2, '0') + ':' + String(secs).padStart(2, '0');

            if (maxSeconds) {
                var remaining = Math.max(0, maxSeconds - elapsed);
                var remMins = Math.floor(remaining / 60);
                document.getElementById('timeRemaining').textContent = remMins + ' min remaining';
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

        function startOrchestra() {
            var projectPath = document.getElementById('projectPath').value;
            var maxHours = document.getElementById('maxHours').value;

            if (!projectPath) {
                alert('Please enter a project path');
                return;
            }

            socket.emit('start_orchestra', {
                project_path: projectPath,
                max_hours: parseFloat(maxHours)
            });
        }

        function stopOrchestra() {
            socket.emit('stop_orchestra');
        }
    </script>
</body>
</html>
"""

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
                        socketio.emit('state_update', orchestra_state)
        except Exception as e:
            pass
        time.sleep(30)

@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/api/state')
def get_state():
    return jsonify(orchestra_state)

@socketio.on('connect')
def handle_connect():
    emit('state_update', orchestra_state)

@socketio.on('get_state')
def handle_get_state():
    emit('state_update', orchestra_state)

@socketio.on('start_orchestra')
def handle_start(data):
    global orchestra_state

    if orchestra_state["running"]:
        return

    project_path = data.get('project_path')
    max_hours = data.get('max_hours', 1)

    if not project_path or not os.path.exists(project_path):
        emit('log_line', {'line': 'Error: Invalid project path: ' + str(project_path)})
        return

    orchestra_state["running"] = True
    orchestra_state["project_path"] = project_path
    orchestra_state["start_time"] = datetime.now().isoformat()
    orchestra_state["max_hours"] = max_hours
    orchestra_state["current_cycle"] = 0
    orchestra_state["cycles_completed"] = 0
    orchestra_state["prs_created"] = []
    orchestra_state["log_lines"] = []

    emit('state_update', orchestra_state)
    emit('log_line', {'line': 'Starting Claude Orchestra on ' + project_path})
    emit('log_line', {'line': 'Max runtime: ' + str(max_hours) + ' hour(s)'})

    def run_orchestra():
        script_dir = os.path.dirname(os.path.abspath(__file__))
        log_file = os.path.join(script_dir, 'claude_orchestra_stream.log')

        open(log_file, 'w').close()

        cmd = [
            'python3',
            os.path.join(script_dir, 'claude_orchestra.py'),
            '--project', project_path,
            '--continuous',
            '--max-hours', str(max_hours),
            '--timeout', '600'
        ]

        orchestra_state["process"] = subprocess.Popen(
            cmd,
            cwd=script_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        for line in orchestra_state["process"].stdout:
            if not orchestra_state["running"]:
                break
            socketio.emit('log_line', {'line': line.strip()})

            if '[STAGE 1]' in line:
                orchestra_state["current_stage"] = "implement"
            elif '[STAGE 2]' in line:
                orchestra_state["current_stage"] = "test"
            elif '[STAGE 3]' in line:
                orchestra_state["current_stage"] = "review"
            elif '[STAGE 4]' in line:
                orchestra_state["current_stage"] = "plan"
            elif 'CYCLE' in line and '/' in line:
                try:
                    cycle = int(line.split('CYCLE')[1].split('/')[0].strip())
                    orchestra_state["current_cycle"] = cycle
                except:
                    pass
            elif 'Cycle' in line and 'complete' in line:
                orchestra_state["cycles_completed"] += 1

            socketio.emit('state_update', orchestra_state)

        orchestra_state["running"] = False
        orchestra_state["current_stage"] = None
        socketio.emit('state_update', orchestra_state)
        socketio.emit('log_line', {'line': 'Orchestra stopped'})

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
    emit('state_update', orchestra_state)
    emit('log_line', {'line': 'Stopping orchestra...'})

if __name__ == '__main__':
    print("=" * 50)
    print("Claude Orchestra Dashboard")
    print("=" * 50)
    print("")
    print("Open http://localhost:5050 in your browser")
    print("")
    socketio.run(app, host='0.0.0.0', port=5050, debug=False)
