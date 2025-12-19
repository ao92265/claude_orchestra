#!/usr/bin/env python3
"""
Claude Orchestra - Multi-Agent Development Pipeline

Orchestrates multiple Claude Code instances running autonomously to handle
different parts of the software development lifecycle:
- Feature Implementation
- Testing & PR Creation
- Code Review
- Feature Ideation & Planning

Usage:
    python claude_orchestra.py --project /path/to/project --cycle

Requirements:
    - Claude Code CLI installed and authenticated
    - Project with a TODO.md file for task tracking
"""

import subprocess
import argparse
import json
import logging
import time
import atexit
import signal
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from datetime import datetime
from process_manager import get_process_manager

# Configure logging with immediate flush for real-time streaming
class FlushingStreamHandler(logging.StreamHandler):
    def emit(self, record):
        super().emit(record)
        self.flush()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('claude_orchestra.log'),
        FlushingStreamHandler()  # Flushes after every log for real-time output
    ]
)
logger = logging.getLogger(__name__)

# Initialize process manager for cleanup
process_manager = get_process_manager()

# Register cleanup on exit
def cleanup_on_exit():
    """Clean up all spawned processes when orchestra exits."""
    logger.info("Orchestra exiting, cleaning up processes...")
    process_manager.stop_all_processes(timeout=5)
    orphan_count = process_manager.detect_and_kill_orphans()
    if orphan_count > 0:
        logger.info(f"Cleaned up {orphan_count} orphaned process(es) on exit")

atexit.register(cleanup_on_exit)


class AgentRole(Enum):
    """Different agent roles in the development pipeline."""
    IMPLEMENTER = "implementer"
    TESTER = "tester"
    REVIEWER = "reviewer"
    PLANNER = "planner"


@dataclass
class AgentResult:
    """Result from an agent execution."""
    role: AgentRole
    success: bool
    output: str
    error: Optional[str] = None
    pr_number: Optional[int] = None
    branch_name: Optional[str] = None
    review_decision: Optional[str] = None  # "APPROVED" or "CHANGES_REQUESTED"
    review_feedback: Optional[str] = None  # Extracted feedback for fixes


class ClaudeOrchestra:
    """
    Orchestrates multiple Claude Code instances for autonomous development.

    This class manages a pipeline of specialized Claude agents that work
    together to implement features, test code, review PRs, and plan work.
    """

    def __init__(
        self,
        project_path: str,
        todo_file: str = "TODO.md",
        timeout: int = 1800,  # 30 minutes default per agent
        model: str = "sonnet",  # or "opus" for more complex tasks
        planner_model: str = "haiku",  # OPTIMIZED: Use cheaper model for planner
        stream: bool = True,  # Stream output in real-time
        task_mode: str = "normal",  # "small", "normal", or "large"
        guidance: str = "",  # Initial guidance for the implementer
        task_queue: list = None,  # List of tasks to work on first
        use_subagents: bool = False  # Use specialized sub-agents
    ):
        self.project_path = Path(project_path).resolve()
        self.todo_file = self.project_path / todo_file
        self.timeout = timeout
        self.model = model
        self.planner_model = planner_model  # Separate model for planner (cost optimization)
        self.stream = stream
        self.task_mode = task_mode
        self.guidance = guidance
        self.task_queue = task_queue or []
        self.current_task_index = 0  # Track which queued task we're on
        self.use_subagents = use_subagents

        # Initialize ProcessManager for subprocess lifecycle management
        self.process_manager = get_process_manager()

        # Validate project path
        if not self.project_path.exists():
            raise ValueError(f"Project path does not exist: {self.project_path}")

        logger.info(f"Initialized Claude Orchestra for project: {self.project_path}")
        logger.info(f"Models: main={self.model}, planner={self.planner_model}")
        if self.guidance:
            logger.info(f"Initial guidance: {self.guidance[:100]}...")
        if self.task_queue:
            logger.info(f"Task queue: {len(self.task_queue)} task(s)")
        if self.use_subagents:
            logger.info("Sub-agents ENABLED: Will use specialized agents for review, testing, and debugging")

    def _get_subagent_instructions(self, role: str) -> str:
        """
        Get sub-agent usage instructions for a specific role.

        These instructions tell Claude to leverage specialized sub-agents
        available through the Task tool for better results.

        OPTIMIZED: Consolidated from ~50 lines to ~10 lines total.
        Saves ~400-500 tokens per cycle while maintaining effectiveness.
        """
        if not self.use_subagents:
            return ""

        # Shared base instruction - available to all roles
        base = "SUB-AGENTS: Use Task tool with subagent_type for: Explore (codebase search), debugger (errors), code-reviewer (quality), Plan (architecture)."

        # Role-specific hints (single line each)
        role_hints = {
            "implementer": "Also: backend-architect, database-optimizer for specialized work. Self-review before committing.",
            "tester": "Also: test-automator, performance-engineer. Investigate failures thoroughly.",
            "reviewer": "Also: security-auditor, architect-review, performance-engineer. Use multiple for comprehensive review.",
            "planner": "Also: architect-review, dx-optimizer, docs-architect. Discover improvement opportunities.",
            "fixer": "Verify fixes with code-reviewer before pushing."
        }

        hint = role_hints.get(role, "")
        return f"\n{base} {hint} Use proactively!\n"

    def _run_claude(self, prompt: str, working_dir: Optional[Path] = None, model_override: Optional[str] = None) -> AgentResult:
        """
        Run Claude Code CLI in headless mode with autonomous permissions.

        Args:
            prompt: The task prompt to send to Claude
            working_dir: Working directory for the command (defaults to project_path)
            model_override: Optional model to use instead of self.model (for cost optimization)

        Returns:
            AgentResult with the execution results
        """
        cwd = working_dir or self.project_path
        model = model_override or self.model

        if self.stream:
            cmd = [
                "claude",
                "-p", prompt,
                "--dangerously-skip-permissions",
                "--model", model,
                "--output-format", "stream-json",
                "--verbose"  # Required for stream-json with -p
            ]
        else:
            cmd = [
                "claude",
                "-p", prompt,
                "--dangerously-skip-permissions",
                "--model", model,
                "--output-format", "text"
            ]

        logger.info(f"Running Claude ({model}) with prompt: {prompt[:100]}...")

        if self.stream:
            return self._run_claude_streaming(cmd, cwd)
        else:
            return self._run_claude_captured(cmd, cwd)

    def _run_claude_streaming(self, cmd: list, cwd: Path) -> AgentResult:
        """Run Claude with real-time streaming using stream-json format."""
        import threading
        import queue
        import uuid

        output_queue = queue.Queue()
        full_output = []
        result_text = ""
        start_time = time.time()
        process_id = f"claude-stream-{uuid.uuid4().hex[:8]}"

        def timestamp():
            """Get current timestamp."""
            return datetime.now().strftime("%H:%M:%S")

        def log_print(msg, end="\n", flush=True):
            """Print with timestamp and log to file."""
            if end == "\n":
                line = f"[{timestamp()}] {msg}"
            else:
                line = msg  # For streaming text, no timestamp
            print(line, end=end, flush=flush)
            # Also write to stream log file
            try:
                with open("claude_orchestra_stream.log", "a") as f:
                    f.write(line + (end if end != "\n" else "\n"))
            except:
                pass

        def read_stream(stream, stream_type):
            """Read from stream and put lines into queue."""
            try:
                for line in iter(stream.readline, ''):
                    if line:
                        output_queue.put((stream_type, line.strip()))
                stream.close()
            except Exception:
                pass

        def parse_and_display(line):
            """Parse stream-json event and display relevant info."""
            nonlocal result_text
            try:
                event = json.loads(line)
                event_type = event.get("type", "")

                # Assistant text output
                if event_type == "assistant":
                    msg = event.get("message", {})
                    for block in msg.get("content", []):
                        if block.get("type") == "text":
                            text = block.get("text", "")
                            log_print(f"  {text}")
                            result_text += text + "\n"

                # Text being generated (streaming delta)
                elif event_type == "content_block_delta":
                    delta = event.get("delta", {})
                    if delta.get("type") == "text_delta":
                        text = delta.get("text", "")
                        log_print(text, end="")
                        result_text += text

                # Tool use (shows what Claude is doing)
                elif event_type == "tool_use":
                    tool_name = event.get("name", "unknown")
                    log_print(f"\n  [TOOL] {tool_name}")

                # Content block start
                elif event_type == "content_block_start":
                    block = event.get("content_block", {})
                    if block.get("type") == "tool_use":
                        tool_name = block.get("name", "")
                        log_print(f"[TOOL] Using: {tool_name}")
                    elif block.get("type") == "text":
                        pass  # Text blocks handled by delta

                # System messages
                elif event_type == "system":
                    log_print(f"[SYSTEM] {event.get('message', '')}")

                # Result/final message
                elif event_type == "result":
                    result_text = event.get("result", result_text)

                return True
            except json.JSONDecodeError:
                # Not JSON, just print raw
                log_print(f"  {line}")
                return True
            except Exception as e:
                return True

        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1
            )

            # Track the process with ProcessManager for automatic cleanup
            self.process_manager.track_process(process_id, process)

            log_print("=" * 60)
            log_print("CLAUDE WORKING (live stream)")
            log_print("=" * 60)

            # Start reader threads
            stdout_thread = threading.Thread(target=read_stream, args=(process.stdout, 'stdout'))
            stderr_thread = threading.Thread(target=read_stream, args=(process.stderr, 'stderr'))
            stdout_thread.daemon = True
            stderr_thread.daemon = True
            stdout_thread.start()
            stderr_thread.start()

            # Process output as it arrives
            last_activity = time.time()
            while True:
                elapsed = time.time() - start_time
                if elapsed > self.timeout:
                    # Use ProcessManager for graceful shutdown with timeout
                    logger.warning(f"Claude execution timeout ({self.timeout}s), stopping process...")
                    if not self.process_manager.stop_process(process_id, timeout=10):
                        logger.error("Failed to stop process gracefully")
                    return AgentResult(
                        role=AgentRole.IMPLEMENTER,
                        success=False,
                        output=result_text,
                        error=f"Execution timed out after {self.timeout} seconds"
                    )

                # Show heartbeat every 30s of no activity
                if time.time() - last_activity > 30:
                    log_print(f"... working ({int(elapsed)}s) ...")
                    last_activity = time.time()

                try:
                    stream_type, line = output_queue.get(timeout=0.5)
                    last_activity = time.time()
                    if stream_type == 'stdout' and line:
                        full_output.append(line)
                        parse_and_display(line)
                    elif stream_type == 'stderr' and line:
                        log_print(f"[stderr] {line}")
                except queue.Empty:
                    pass

                # Check if done
                if process.poll() is not None and not stdout_thread.is_alive() and not stderr_thread.is_alive():
                    while not output_queue.empty():
                        try:
                            stream_type, line = output_queue.get_nowait()
                            if stream_type == 'stdout' and line:
                                full_output.append(line)
                                parse_and_display(line)
                        except queue.Empty:
                            break
                    break

            log_print("=" * 60)
            elapsed = time.time() - start_time
            log_print(f"Completed in {elapsed:.1f}s")
            log_print("=" * 60)

            success = process.returncode == 0
            if success:
                logger.info("Claude execution completed successfully")
            else:
                logger.error(f"Claude execution failed with code {process.returncode}")

            # Untrack process after completion
            self.process_manager.untrack_process(process_id)

            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=success,
                output=result_text or "\n".join(full_output),
                error=None if success else f"Exit code: {process.returncode}"
            )

        except FileNotFoundError:
            logger.error("Claude CLI not found. Is it installed?")
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=False,
                output="",
                error="Claude CLI not found. Please install it first."
            )
        except Exception as e:
            # Ensure cleanup on any unexpected error
            logger.error(f"Unexpected error in _run_claude_streaming: {e}")
            self.process_manager.untrack_process(process_id)
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=False,
                output=result_text,
                error=str(e)
            )

    def _run_claude_captured(self, cmd: list, cwd: Path) -> AgentResult:
        """Run Claude with captured output (no streaming)."""
        import uuid
        
        process_id = f"claude-captured-{uuid.uuid4().hex[:8]}"
        
        try:
            # Use Popen instead of run() for ProcessManager integration
            process = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True
            )
            
            # Track the process
            self.process_manager.track_process(process_id, process)
            
            try:
                # Wait for completion with timeout
                stdout, stderr = process.communicate(timeout=self.timeout)
                success = process.returncode == 0
                
                if success:
                    logger.info("Claude execution completed successfully")
                else:
                    logger.error(f"Claude execution failed: {stderr}")
                
                # Untrack after completion
                self.process_manager.untrack_process(process_id)
                
                return AgentResult(
                    role=AgentRole.IMPLEMENTER,
                    success=success,
                    output=stdout,
                    error=stderr if not success else None
                )
                
            except subprocess.TimeoutExpired:
                logger.error(f"Claude execution timed out after {self.timeout}s")
                # Use ProcessManager for graceful shutdown
                self.process_manager.stop_process(process_id, timeout=10)
                return AgentResult(
                    role=AgentRole.IMPLEMENTER,
                    success=False,
                    output="",
                    error=f"Execution timed out after {self.timeout} seconds"
                )
                
        except FileNotFoundError:
            logger.error("Claude CLI not found. Is it installed?")
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=False,
                output="",
                error="Claude CLI not found. Please install it first."
            )
        except Exception as e:
            logger.error(f"Unexpected error in _run_claude_captured: {e}")
            self.process_manager.untrack_process(process_id)
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=False,
                output="",
                error=str(e)
            )

    def run_implementer(self, task_description: Optional[str] = None) -> AgentResult:
        """
        Run the Implementer agent to pick up and implement a task from TODO.

        This agent reads the TODO list, selects the highest priority task,
        implements it, and creates a feature branch with the changes.

        If a task_queue was provided, it will work through those tasks first
        before reverting to autonomous task selection from TODO.md.
        """
        # Check if we have queued tasks to work on
        queued_task = None
        if self.task_queue and self.current_task_index < len(self.task_queue):
            queued_task = self.task_queue[self.current_task_index]
            self.current_task_index += 1
            logger.info(f"Working on queued task {self.current_task_index}/{len(self.task_queue)}: {queued_task[:60]}...")

        # Build user guidance section
        user_guidance_section = ""
        if self.guidance:
            user_guidance_section = f"""
USER GUIDANCE (follow these instructions carefully):
{self.guidance}
"""

        # Build task guidance based on mode
        if self.task_mode == "small":
            task_guidance = """IMPORTANT: Choose tasks that can realistically be completed in under 20 minutes.
Skip very large tasks (like "Super Admin Portal" or "Full System Redesign") and pick smaller, focused tasks instead.
If all remaining tasks are too large, break one down into subtasks and implement just the first subtask."""
        elif self.task_mode == "large":
            task_guidance = """You are authorized to work on LARGE, complex tasks. Take your time and be thorough.
Select the highest priority task from TODO.md regardless of size - even multi-file features are fine.
For very large tasks, implement them systematically, committing progress as you go.
You have plenty of time, so focus on quality and completeness."""
        else:  # normal
            task_guidance = """Select appropriately sized tasks - not trivial one-liners, but not massive multi-day features either.
If a task seems very large, consider implementing a meaningful subset and noting what remains."""

        # Build the task selection instructions based on whether we have a queued task
        if queued_task:
            task_selection = f"""SPECIFIC TASK TO IMPLEMENT (from user's queue):
{queued_task}

This task was specifically selected by the user. Implement it as described.
Find this task in TODO.md (or a related planning document) and mark it appropriately."""
        elif task_description:
            task_selection = f"""SPECIFIC TASK TO IMPLEMENT:
{task_description}"""
        else:
            task_selection = f"""TASK SELECTION:
Look for TODO/task files in these locations (check all that exist):
- TODO.md (root)
- docs/TODO.md
- docs/TASKS.md
- docs/WORK_ALLOCATION.md

Find incomplete tasks (marked with [ ] or - [ ] or similar).
Select the most appropriate task based on priority and the guidance above.

{task_guidance}"""

        subagent_instructions = self._get_subagent_instructions("implementer")

        prompt = f"""You are the IMPLEMENTER agent in an autonomous development pipeline.
{user_guidance_section}
Your task: Implement the specified task (or select one from TODO.md if none specified).

{task_selection}
{subagent_instructions}
Instructions:
1. {"Find the specified task in TODO.md or docs/TODO.md" if queued_task else "Check TODO.md, docs/TODO.md, docs/TASKS.md for incomplete tasks"}
2. IMMEDIATELY mark the task as in-progress in the TODO file (wherever you found it) and commit this change
3. Create a new feature branch with a descriptive name
4. Implement the feature/fix thoroughly
5. Mark the task as complete in the TODO file
6. Commit your changes with clear commit messages
7. When done, output a summary including:
   - BRANCH_NAME: <the branch you created>
   - TASK_COMPLETED: <description of what was implemented>
   - FILES_CHANGED: <list of modified files>

Work autonomously until the implementation is complete. Be thorough but pragmatic.
"""

        result = self._run_claude(prompt)
        result.role = AgentRole.IMPLEMENTER

        # Try to extract branch name from output
        if result.success and "BRANCH_NAME:" in result.output:
            for line in result.output.split('\n'):
                if line.startswith("BRANCH_NAME:"):
                    result.branch_name = line.split(":", 1)[1].strip()
                    break

        return result

    def run_tester(self, branch_name: Optional[str] = None) -> AgentResult:
        """
        Run the Tester agent to test changes and create a PR if tests pass.

        This agent runs tests, fixes any issues, and creates a pull request
        when everything is working.
        """
        branch_context = f"on branch '{branch_name}'" if branch_name else "on the current feature branch"
        subagent_instructions = self._get_subagent_instructions("tester")

        prompt = f"""You are the TESTER agent in an autonomous development pipeline.

Your task: Thoroughly test the recent changes {branch_context} and create a PR if tests pass.
{subagent_instructions}
Instructions:
1. Identify the current feature branch (or checkout {branch_name} if specified)
2. Run the project's test suite (look for pytest, npm test, etc.)
3. If tests fail:
   - Analyze the failures
   - Fix the issues
   - Re-run tests until they pass
4. Run any linting/type checking tools
5. If everything passes, create a Pull Request:
   - Use `gh pr create` or similar
   - Write a comprehensive PR description
   - Include test results summary
6. Output a summary including:
   - PR_NUMBER: <the PR number created>
   - TEST_RESULTS: <summary of test results>
   - ISSUES_FIXED: <any issues you fixed>

Be thorough with testing. Check edge cases. Don't create a PR until tests pass.
"""

        result = self._run_claude(prompt)
        result.role = AgentRole.TESTER
        result.branch_name = branch_name

        # Try to extract PR number from output
        if result.success and "PR_NUMBER:" in result.output:
            for line in result.output.split('\n'):
                if line.startswith("PR_NUMBER:"):
                    try:
                        result.pr_number = int(line.split(":", 1)[1].strip().replace("#", ""))
                    except ValueError:
                        pass
                    break

        return result

    def run_reviewer(self, pr_number: Optional[int] = None) -> AgentResult:
        """
        Run the Reviewer agent to review a pull request.

        This agent performs a thorough code review and either approves
        the PR or requests changes with specific feedback.
        """
        pr_context = f"PR #{pr_number}" if pr_number else "the most recent open PR"
        subagent_instructions = self._get_subagent_instructions("reviewer")

        prompt = f"""You are the REVIEWER agent in an autonomous development pipeline.

Your task: Perform a thorough code review of {pr_context}.
{subagent_instructions}
Instructions:
1. Fetch the PR details using `gh pr view {pr_number if pr_number else ''}`
2. Review the diff using `gh pr diff {pr_number if pr_number else ''}`
3. Check for:
   - Code quality and readability
   - Potential bugs or edge cases
   - Security vulnerabilities
   - Performance issues
   - Test coverage
   - Documentation
4. If issues found:
   - Leave specific, actionable comments
   - Request changes with clear explanations
5. If the code looks good:
   - Approve the PR with `gh pr review --approve`
   - Add a constructive approval comment
6. Output a summary including:
   - REVIEW_DECISION: APPROVED or CHANGES_REQUESTED
   - KEY_FEEDBACK: <main points from your review>
   - ISSUES_FOUND: <list of issues if any>

Be constructive but thorough. Good code review improves the entire codebase.
"""

        result = self._run_claude(prompt)
        result.role = AgentRole.REVIEWER
        result.pr_number = pr_number

        # Parse review decision and feedback
        self._parse_review_output(result)

        return result

    def _parse_review_output(self, result: AgentResult) -> None:
        """
        Extract review decision and feedback from reviewer output.

        Looks for:
        - REVIEW_DECISION: APPROVED or CHANGES_REQUESTED
        - KEY_FEEDBACK: <feedback text>
        - ISSUES_FOUND: <list of issues>
        """
        output = result.output
        feedback_parts = []

        for line in output.split('\n'):
            line_stripped = line.strip()

            # Extract decision
            if line_stripped.startswith("REVIEW_DECISION:"):
                decision = line_stripped.split(":", 1)[1].strip().upper()
                if "APPROVED" in decision:
                    result.review_decision = "APPROVED"
                elif "CHANGES" in decision or "REQUEST" in decision:
                    result.review_decision = "CHANGES_REQUESTED"

            # Extract feedback
            elif line_stripped.startswith("KEY_FEEDBACK:"):
                feedback_parts.append(line_stripped.split(":", 1)[1].strip())
            elif line_stripped.startswith("ISSUES_FOUND:"):
                feedback_parts.append(line_stripped.split(":", 1)[1].strip())

        # Combine feedback for the fixer agent
        if feedback_parts:
            result.review_feedback = " | ".join(feedback_parts)

        # If we couldn't parse a decision, try to infer from keywords
        if not result.review_decision:
            output_lower = output.lower()
            if "approve" in output_lower and "request" not in output_lower:
                result.review_decision = "APPROVED"
            elif "changes requested" in output_lower or "request changes" in output_lower:
                result.review_decision = "CHANGES_REQUESTED"

        logger.info(f"Review decision: {result.review_decision}")
        if result.review_feedback:
            logger.info(f"Review feedback: {result.review_feedback[:100]}...")

    def run_fixer(self, branch_name: str, review_feedback: str, pr_number: int = None) -> AgentResult:
        """
        Run the Fixer agent to address review feedback.

        This agent takes the review feedback and makes the necessary changes
        to address the reviewer's concerns.
        """
        subagent_instructions = self._get_subagent_instructions("fixer")

        prompt = f"""You are the FIXER agent in an autonomous development pipeline.

Your task: Address the code review feedback and push fixes to the existing PR.

Branch: {branch_name}
PR Number: {pr_number if pr_number else 'current'}

REVIEW FEEDBACK TO ADDRESS:
{review_feedback}
{subagent_instructions}
Instructions:
1. Checkout the branch: {branch_name}
2. Carefully read and understand each piece of feedback
3. Make the necessary code changes to address ALL feedback points
4. Run tests to ensure your fixes don't break anything
5. Commit your fixes with a clear message referencing the review
6. Push the changes to update the PR
7. Output a summary including:
   - FIXES_APPLIED: <list of changes made>
   - FEEDBACK_ADDRESSED: <which feedback points were addressed>
   - TESTS_PASSED: yes/no

Be thorough - address ALL feedback points. Don't leave anything unresolved.
"""

        result = self._run_claude(prompt)
        result.role = AgentRole.IMPLEMENTER  # Fixer is a specialized implementer
        result.branch_name = branch_name
        result.pr_number = pr_number

        return result

    def run_planner(self) -> AgentResult:
        """
        Run the Planner agent to ideate new features and update the TODO list.

        This agent analyzes the codebase and suggests improvements,
        new features, refactors, or optimizations.

        OPTIMIZED: Uses planner_model (default: haiku) for cost savings.
        Planner tasks don't require complex coding, so a faster/cheaper model works well.
        """
        subagent_instructions = self._get_subagent_instructions("planner")

        prompt = f"""You are the PLANNER agent in an autonomous development pipeline.

Your task: Analyze the codebase and add valuable new tasks to the project's TODO file (docs/TODO.md or TODO.md).
{subagent_instructions}
Instructions:
1. Read the current TODO file (check TODO.md, docs/TODO.md, docs/TASKS.md) to understand existing plans
2. Explore the codebase structure and code quality
3. Identify opportunities for:
   - New features that would add value
   - Performance optimizations
   - Code refactoring for better maintainability
   - Security improvements
   - Test coverage gaps
   - Documentation improvements
   - Technical debt reduction
4. For each idea, assess:
   - Impact (high/medium/low)
   - Effort (high/medium/low)
   - Priority based on impact/effort ratio
5. Add 3-5 new well-described tasks to the project's TODO file:
   - Clear, actionable descriptions
   - Priority indicators
   - Brief rationale
6. Output a summary including:
   - TASKS_ADDED: <list of new tasks>
   - RATIONALE: <why these tasks matter>
   - CODEBASE_HEALTH: <overall assessment>

Focus on practical improvements that will genuinely help the project.
"""

        # Use planner-specific model (default: haiku) for cost optimization
        result = self._run_claude(prompt, model_override=self.planner_model)
        result.role = AgentRole.PLANNER

        return result

    def run_full_cycle(self, max_review_iterations: int = 3) -> dict:
        """
        Run a complete development cycle through all agents.

        Pipeline: Implementer -> Tester -> Reviewer -> (Fixer if needed) -> Planner

        If the reviewer requests changes, automatically runs the Fixer agent
        and re-submits for review, up to max_review_iterations times.

        Args:
            max_review_iterations: Maximum times to retry after review feedback (default: 3)

        Returns:
            Dictionary with results from each stage
        """
        logger.info("=" * 60)
        logger.info("Starting full development cycle")
        logger.info(f"Max review iterations: {max_review_iterations}")
        logger.info("=" * 60)

        results = {}

        # Stage 1: Implementation
        logger.info("\n[STAGE 1] Running Implementer Agent...")
        impl_result = self.run_implementer()
        results['implementer'] = impl_result

        if not impl_result.success:
            logger.error("Implementation failed, stopping cycle")
            return results

        branch_name = impl_result.branch_name

        # Stage 2: Testing & PR Creation
        logger.info("\n[STAGE 2] Running Tester Agent...")
        test_result = self.run_tester(branch_name=branch_name)
        results['tester'] = test_result

        if not test_result.success:
            logger.error("Testing failed, stopping cycle")
            return results

        pr_number = test_result.pr_number

        # Stage 3: Code Review (with retry loop)
        review_iteration = 0
        review_result = None
        fix_results = []

        while review_iteration < max_review_iterations:
            review_iteration += 1
            logger.info(f"\n[STAGE 3] Running Reviewer Agent (iteration {review_iteration}/{max_review_iterations})...")

            review_result = self.run_reviewer(pr_number=pr_number)

            # Check the review decision
            if review_result.review_decision == "APPROVED":
                logger.info("PR APPROVED! Moving to planning stage.")
                break

            elif review_result.review_decision == "CHANGES_REQUESTED":
                logger.warning(f"Changes requested by reviewer")

                if review_iteration >= max_review_iterations:
                    logger.error(f"Max review iterations ({max_review_iterations}) reached. Stopping.")
                    break

                # Extract feedback and run fixer
                feedback = review_result.review_feedback or "Please review the PR comments and address all issues."
                logger.info(f"\n[STAGE 3.{review_iteration}] Running Fixer Agent...")
                logger.info(f"Feedback to address: {feedback[:200]}...")

                fix_result = self.run_fixer(
                    branch_name=branch_name,
                    review_feedback=feedback,
                    pr_number=pr_number
                )
                fix_results.append(fix_result)

                if not fix_result.success:
                    logger.error("Fixer agent failed, stopping cycle")
                    break

                logger.info("Fixes applied, requesting re-review...")

            else:
                # Couldn't determine decision, assume we need to stop
                logger.warning("Could not determine review decision, assuming needs manual review")
                break

        # Store review results
        results['reviewer'] = review_result
        if fix_results:
            results['fix_iterations'] = fix_results
            results['total_review_iterations'] = review_iteration

        # Stage 4: Planning (only on successful cycles to save tokens)
        # OPTIMIZED: Skip planner when cycle failed - saves ~1,800-3,500 tokens
        cycle_successful = (
            review_result is not None and
            review_result.review_decision == "APPROVED"
        )

        if cycle_successful:
            logger.info("\n[STAGE 4] Running Planner Agent...")
            plan_result = self.run_planner()
            results['planner'] = plan_result
        else:
            logger.info("\n[STAGE 4] Skipping Planner Agent (cycle not successful)")
            logger.info("Planner runs only on approved PRs to optimize token usage")
            results['planner_skipped'] = True

        # Final summary
        logger.info("\n" + "=" * 60)
        logger.info("Development cycle complete!")
        logger.info("=" * 60)

        if review_result and review_result.review_decision:
            if review_result.review_decision == "APPROVED":
                logger.info(f"PR #{pr_number} was APPROVED after {review_iteration} review(s)")
            else:
                logger.warning(f"PR #{pr_number} still needs attention after {review_iteration} review(s)")

        return results

    def run_continuous(self, max_cycles: int = 10, delay_between_cycles: int = 60, max_hours: float = None):
        """
        Run continuous development cycles.

        Args:
            max_cycles: Maximum number of cycles to run (safety limit)
            delay_between_cycles: Seconds to wait between cycles
            max_hours: Maximum hours to run (optional time limit)
        """
        start_time = time.time()
        max_seconds = max_hours * 3600 if max_hours else None

        logger.info(f"Starting continuous mode: max {max_cycles} cycles")
        if max_hours:
            logger.info(f"Time limit: {max_hours} hour(s)")

        for cycle in range(1, max_cycles + 1):
            # Check time limit
            if max_seconds and (time.time() - start_time) >= max_seconds:
                elapsed_hours = (time.time() - start_time) / 3600
                logger.info(f"Time limit reached ({elapsed_hours:.1f} hours). Stopping.")
                break

            logger.info(f"\n{'#' * 60}")
            logger.info(f"CYCLE {cycle}/{max_cycles}")
            if max_hours:
                remaining = max_seconds - (time.time() - start_time)
                logger.info(f"Time remaining: {remaining/60:.0f} minutes")
            logger.info(f"{'#' * 60}")

            try:
                results = self.run_full_cycle()

                # Log cycle summary
                successful_stages = sum(
                    1 for r in results.values() if hasattr(r, 'success') and r.success
                )
                logger.info(
                    f"Cycle {cycle} complete: {successful_stages}/{len(results)} stages successful"
                )

            except KeyboardInterrupt:
                logger.info("Received interrupt, stopping continuous mode")
                break
            except Exception as e:
                logger.error(f"Cycle {cycle} failed with error: {e}")

            if cycle < max_cycles:
                logger.info(f"Waiting {delay_between_cycles}s before next cycle...")
                time.sleep(delay_between_cycles)


def create_sample_todo(project_path: Path):
    """Create a sample TODO.md file for demonstration."""
    todo_content = """# Project TODO

## High Priority
- [ ] Implement user authentication system
- [ ] Add input validation for API endpoints
- [ ] Set up CI/CD pipeline

## Medium Priority
- [ ] Refactor database queries for better performance
- [ ] Add comprehensive error handling
- [ ] Write unit tests for core modules

## Low Priority
- [ ] Update documentation
- [ ] Add logging throughout the application
- [ ] Consider caching strategy

## Completed
- [x] Initial project setup
- [x] Basic routing structure
"""

    todo_file = project_path / "TODO.md"
    if not todo_file.exists():
        todo_file.write_text(todo_content)
        logger.info(f"Created sample TODO.md at {todo_file}")
    else:
        logger.info(f"TODO.md already exists at {todo_file}")


def main():
    parser = argparse.ArgumentParser(
        description="Claude Orchestra - Multi-Agent Development Pipeline",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Run a single full cycle
  python claude_orchestra.py --project /path/to/project --cycle

  # Run only the implementer agent
  python claude_orchestra.py --project /path/to/project --implement

  # Run only the tester agent on a specific branch
  python claude_orchestra.py --project /path/to/project --test --branch feature/new-auth

  # Run continuous mode (be careful!)
  python claude_orchestra.py --project /path/to/project --continuous --max-cycles 5

  # Create a sample TODO.md file
  python claude_orchestra.py --project /path/to/project --init-todo
        """
    )

    parser.add_argument(
        "--project", "-P",
        required=True,
        help="Path to the project directory"
    )
    parser.add_argument(
        "--cycle",
        action="store_true",
        help="Run a full development cycle (implement -> test -> review -> plan)"
    )
    parser.add_argument(
        "--continuous",
        action="store_true",
        help="Run continuous development cycles"
    )
    parser.add_argument(
        "--max-cycles",
        type=int,
        default=10,
        help="Maximum cycles for continuous mode (default: 10)"
    )
    parser.add_argument(
        "--implement",
        action="store_true",
        help="Run only the implementer agent"
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Run only the tester agent"
    )
    parser.add_argument(
        "--review",
        action="store_true",
        help="Run only the reviewer agent"
    )
    parser.add_argument(
        "--plan",
        action="store_true",
        help="Run only the planner agent"
    )
    parser.add_argument(
        "--branch",
        help="Branch name for tester agent"
    )
    parser.add_argument(
        "--pr",
        type=int,
        help="PR number for reviewer agent"
    )
    parser.add_argument(
        "--task",
        help="Specific task description for implementer agent"
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=1800,
        help="Timeout per agent in seconds (default: 1800 = 30 min)"
    )
    parser.add_argument(
        "--model",
        choices=["sonnet", "opus", "haiku"],
        default="sonnet",
        help="Claude model for main agents (default: sonnet)"
    )
    parser.add_argument(
        "--planner-model",
        choices=["sonnet", "opus", "haiku"],
        default="haiku",
        help="Claude model for planner agent (default: haiku for cost savings)"
    )
    parser.add_argument(
        "--task-mode",
        choices=["small", "normal", "large"],
        default="normal",
        help="Task size mode: small (quick fixes), normal (balanced), large (big features)"
    )
    parser.add_argument(
        "--init-todo",
        action="store_true",
        help="Create a sample TODO.md file"
    )
    parser.add_argument(
        "--max-review-iterations",
        type=int,
        default=3,
        help="Max times to retry after review feedback (default: 3)"
    )
    parser.add_argument(
        "--no-stream",
        action="store_true",
        help="Disable real-time output streaming (capture output instead)"
    )
    parser.add_argument(
        "--max-hours",
        type=float,
        help="Maximum hours to run in continuous mode (e.g., 1 for 1 hour)"
    )
    parser.add_argument(
        "--guidance",
        type=str,
        default="",
        help="Initial guidance/instructions for the implementer agent"
    )
    parser.add_argument(
        "--task-queue",
        type=str,
        default="",
        help="JSON array of tasks to work on (e.g., '[\"task1\", \"task2\"]')"
    )
    parser.add_argument(
        "--use-subagents",
        action="store_true",
        help="Enable use of specialized sub-agents (code-reviewer, test-automator, debugger, etc.)"
    )

    args = parser.parse_args()

    project_path = Path(args.project).resolve()

    # Handle --init-todo separately
    if args.init_todo:
        create_sample_todo(project_path)
        return

    # Parse task queue from JSON if provided
    task_queue = []
    if args.task_queue:
        try:
            task_queue = json.loads(args.task_queue)
            if not isinstance(task_queue, list):
                task_queue = []
        except json.JSONDecodeError:
            logger.warning("Invalid task-queue JSON, ignoring")
            task_queue = []

    # Initialize orchestra
    try:
        orchestra = ClaudeOrchestra(
            project_path=str(project_path),
            timeout=args.timeout,
            model=args.model,
            planner_model=args.planner_model,
            stream=not args.no_stream,
            task_mode=args.task_mode,
            guidance=args.guidance,
            task_queue=task_queue,
            use_subagents=args.use_subagents
        )
    except ValueError as e:
        logger.error(str(e))
        return

    # Run specified mode
    if args.continuous:
        orchestra.run_continuous(max_cycles=args.max_cycles, max_hours=args.max_hours)
    elif args.cycle:
        results = orchestra.run_full_cycle(max_review_iterations=args.max_review_iterations)
        print("\n" + "=" * 60)
        print("CYCLE RESULTS SUMMARY")
        print("=" * 60)

        for key, value in results.items():
            # Handle special keys
            if key == 'fix_iterations':
                print(f"  Fix iterations: {len(value)}")
                continue
            if key == 'total_review_iterations':
                print(f"  Total review rounds: {value}")
                continue
            if key == 'planner_skipped':
                print("SKIP PLANNER: Skipped (PR not approved - saved tokens)")
                continue

            # Regular agent results
            result = value
            status = "OK" if result.success else "FAIL"
            extra = ""
            if hasattr(result, 'review_decision') and result.review_decision:
                extra = f" [{result.review_decision}]"
            print(f"{status} {key.upper()}: {'Success' if result.success else 'Failed'}{extra}")
    elif args.implement:
        result = orchestra.run_implementer(task_description=args.task)
        print(f"\nImplementer Result: {'Success' if result.success else 'Failed'}")
        print(f"Output:\n{result.output}")
    elif args.test:
        result = orchestra.run_tester(branch_name=args.branch)
        print(f"\nTester Result: {'Success' if result.success else 'Failed'}")
        print(f"Output:\n{result.output}")
    elif args.review:
        result = orchestra.run_reviewer(pr_number=args.pr)
        print(f"\nReviewer Result: {'Success' if result.success else 'Failed'}")
        print(f"Output:\n{result.output}")
    elif args.plan:
        result = orchestra.run_planner()
        print(f"\nPlanner Result: {'Success' if result.success else 'Failed'}")
        print(f"Output:\n{result.output}")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
