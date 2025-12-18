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
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from typing import Optional
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('claude_orchestra.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


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
        timeout: int = 600,  # 10 minutes default per agent
        model: str = "sonnet",  # or "opus" for more complex tasks
        stream: bool = True  # Stream output in real-time
    ):
        self.project_path = Path(project_path).resolve()
        self.todo_file = self.project_path / todo_file
        self.timeout = timeout
        self.model = model
        self.stream = stream

        # Validate project path
        if not self.project_path.exists():
            raise ValueError(f"Project path does not exist: {self.project_path}")

        logger.info(f"Initialized Claude Orchestra for project: {self.project_path}")

    def _run_claude(self, prompt: str, working_dir: Optional[Path] = None) -> AgentResult:
        """
        Run Claude Code CLI in headless mode with autonomous permissions.

        Args:
            prompt: The task prompt to send to Claude
            working_dir: Working directory for the command (defaults to project_path)

        Returns:
            AgentResult with the execution results
        """
        cwd = working_dir or self.project_path

        cmd = [
            "claude",
            "-p", prompt,  # Headless mode with prompt
            "--dangerously-skip-permissions",  # Autonomous execution
            "--model", self.model,
            "--output-format", "text"  # Get clean text output
        ]

        logger.info(f"Running Claude with prompt: {prompt[:100]}...")

        if self.stream:
            return self._run_claude_streaming(cmd, cwd)
        else:
            return self._run_claude_captured(cmd, cwd)

    def _run_claude_streaming(self, cmd: list, cwd: Path) -> AgentResult:
        """Run Claude with real-time output streaming to terminal."""
        output_lines = []
        error_lines = []
        start_time = time.time()

        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(cwd),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1  # Line buffered
            )

            print("\n" + "=" * 60)
            print("CLAUDE OUTPUT (streaming)")
            print("=" * 60 + "\n")

            # Read stdout and stderr in real-time
            while True:
                # Check timeout
                if time.time() - start_time > self.timeout:
                    process.kill()
                    process.wait()
                    logger.error(f"Claude execution timed out after {self.timeout}s")
                    return AgentResult(
                        role=AgentRole.IMPLEMENTER,
                        success=False,
                        output="\n".join(output_lines),
                        error=f"Execution timed out after {self.timeout} seconds"
                    )

                # Check if process has finished
                retcode = process.poll()

                # Read available output
                if process.stdout:
                    line = process.stdout.readline()
                    if line:
                        print(f"  {line}", end="", flush=True)
                        output_lines.append(line.rstrip())

                if process.stderr:
                    err_line = process.stderr.readline()
                    if err_line:
                        print(f"  [stderr] {err_line}", end="", flush=True)
                        error_lines.append(err_line.rstrip())

                # If process finished and no more output, break
                if retcode is not None:
                    # Drain remaining output
                    remaining_out, remaining_err = process.communicate()
                    if remaining_out:
                        for line in remaining_out.splitlines():
                            print(f"  {line}")
                            output_lines.append(line)
                    if remaining_err:
                        for line in remaining_err.splitlines():
                            print(f"  [stderr] {line}")
                            error_lines.append(line)
                    break

                time.sleep(0.1)  # Small sleep to prevent busy-waiting

            print("\n" + "=" * 60)
            elapsed = time.time() - start_time
            print(f"Completed in {elapsed:.1f}s")
            print("=" * 60 + "\n")

            success = process.returncode == 0
            output = "\n".join(output_lines)
            error = "\n".join(error_lines) if error_lines and not success else None

            if success:
                logger.info("Claude execution completed successfully")
            else:
                logger.error(f"Claude execution failed: {error}")

            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=success,
                output=output,
                error=error
            )

        except FileNotFoundError:
            logger.error("Claude CLI not found. Is it installed?")
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=False,
                output="",
                error="Claude CLI not found. Please install it first."
            )

    def _run_claude_captured(self, cmd: list, cwd: Path) -> AgentResult:
        """Run Claude with captured output (no streaming)."""
        try:
            result = subprocess.run(
                cmd,
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=self.timeout
            )

            success = result.returncode == 0
            output = result.stdout
            error = result.stderr if result.returncode != 0 else None

            if success:
                logger.info("Claude execution completed successfully")
            else:
                logger.error(f"Claude execution failed: {error}")

            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=success,
                output=output,
                error=error
            )

        except subprocess.TimeoutExpired:
            logger.error(f"Claude execution timed out after {self.timeout}s")
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

    def run_implementer(self, task_description: Optional[str] = None) -> AgentResult:
        """
        Run the Implementer agent to pick up and implement a task from TODO.

        This agent reads the TODO list, selects the highest priority task,
        implements it, and creates a feature branch with the changes.
        """
        prompt = f"""You are the IMPLEMENTER agent in an autonomous development pipeline.

Your task: Read the TODO.md file, select the highest priority incomplete task, and implement it.

Instructions:
1. Read TODO.md to find incomplete tasks (marked with [ ] or similar)
2. Select the most important/urgent task
3. Create a new feature branch with a descriptive name
4. Implement the feature/fix thoroughly
5. Mark the task as in-progress in TODO.md
6. Commit your changes with clear commit messages
7. When done, output a summary including:
   - BRANCH_NAME: <the branch you created>
   - TASK_COMPLETED: <description of what was implemented>
   - FILES_CHANGED: <list of modified files>

{f"Specific task to implement: {task_description}" if task_description else ""}

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

        prompt = f"""You are the TESTER agent in an autonomous development pipeline.

Your task: Thoroughly test the recent changes {branch_context} and create a PR if tests pass.

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

        prompt = f"""You are the REVIEWER agent in an autonomous development pipeline.

Your task: Perform a thorough code review of {pr_context}.

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
        prompt = f"""You are the FIXER agent in an autonomous development pipeline.

Your task: Address the code review feedback and push fixes to the existing PR.

Branch: {branch_name}
PR Number: {pr_number if pr_number else 'current'}

REVIEW FEEDBACK TO ADDRESS:
{review_feedback}

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
        """
        prompt = f"""You are the PLANNER agent in an autonomous development pipeline.

Your task: Analyze the codebase and add valuable new tasks to TODO.md.

Instructions:
1. Read the current TODO.md to understand existing plans
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
5. Add 3-5 new well-described tasks to TODO.md:
   - Clear, actionable descriptions
   - Priority indicators
   - Brief rationale
6. Output a summary including:
   - TASKS_ADDED: <list of new tasks>
   - RATIONALE: <why these tasks matter>
   - CODEBASE_HEALTH: <overall assessment>

Focus on practical improvements that will genuinely help the project.
"""

        result = self._run_claude(prompt)
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

        # Stage 4: Planning (always runs)
        logger.info("\n[STAGE 4] Running Planner Agent...")
        plan_result = self.run_planner()
        results['planner'] = plan_result

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

    def run_continuous(self, max_cycles: int = 10, delay_between_cycles: int = 60):
        """
        Run continuous development cycles.

        Args:
            max_cycles: Maximum number of cycles to run (safety limit)
            delay_between_cycles: Seconds to wait between cycles
        """
        logger.info(f"Starting continuous mode: max {max_cycles} cycles")

        for cycle in range(1, max_cycles + 1):
            logger.info(f"\n{'#' * 60}")
            logger.info(f"CYCLE {cycle}/{max_cycles}")
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
        default=600,
        help="Timeout per agent in seconds (default: 600)"
    )
    parser.add_argument(
        "--model",
        choices=["sonnet", "opus", "haiku"],
        default="sonnet",
        help="Claude model to use (default: sonnet)"
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

    args = parser.parse_args()

    project_path = Path(args.project).resolve()

    # Handle --init-todo separately
    if args.init_todo:
        create_sample_todo(project_path)
        return

    # Initialize orchestra
    try:
        orchestra = ClaudeOrchestra(
            project_path=str(project_path),
            timeout=args.timeout,
            model=args.model,
            stream=not args.no_stream
        )
    except ValueError as e:
        logger.error(str(e))
        return

    # Run specified mode
    if args.continuous:
        orchestra.run_continuous(max_cycles=args.max_cycles)
    elif args.cycle:
        results = orchestra.run_full_cycle(max_review_iterations=args.max_review_iterations)
        print("\n" + "=" * 60)
        print("CYCLE RESULTS SUMMARY")
        print("=" * 60)

        for key, value in results.items():
            # Handle fix_iterations list separately
            if key == 'fix_iterations':
                print(f"  Fix iterations: {len(value)}")
                continue
            if key == 'total_review_iterations':
                print(f"  Total review rounds: {value}")
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
