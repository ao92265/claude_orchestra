#!/usr/bin/env python3
"""
Claude Orchestra (Async Version) - Parallel Multi-Agent Development Pipeline

SECURITY NOTE: This script uses subprocess with explicit argument lists (not shell=True),
which is the safe way to execute commands in Python and prevents command injection.
See: https://docs.python.org/3/library/subprocess.html#security-considerations

Usage:
    python claude_orchestra_async.py --project /path/to/project --pipeline --parallel
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from dataclasses import dataclass, asdict
from enum import Enum
from typing import Optional, List, Dict, Any, Callable
from datetime import datetime

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('claude_orchestra_async.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)


class AgentRole(Enum):
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
    duration_seconds: float = 0.0


@dataclass
class PipelineState:
    """Persistent state for the pipeline."""
    last_run: Optional[str] = None
    total_cycles: int = 0
    successful_cycles: int = 0
    last_branch: Optional[str] = None
    last_pr: Optional[int] = None


class AsyncClaudeOrchestra:
    """
    Async orchestrator for parallel Claude Code agent execution.

    Supports running reviewer and planner in parallel since they
    don't depend on each other.
    """

    def __init__(self, project_path: str, timeout: int = 600, model: str = "sonnet"):
        self.project_path = Path(project_path).resolve()
        self.timeout = timeout
        self.model = model
        self.state_file = self.project_path / ".claude_orchestra_state.json"
        self.state = self._load_state()

        if not self.project_path.exists():
            raise ValueError(f"Project path does not exist: {self.project_path}")

        logger.info(f"Async Orchestra initialized for: {self.project_path}")

    def _load_state(self) -> PipelineState:
        if self.state_file.exists():
            try:
                data = json.loads(self.state_file.read_text())
                return PipelineState(**data)
            except Exception as e:
                logger.warning(f"Failed to load state: {e}")
        return PipelineState()

    def _save_state(self):
        try:
            self.state_file.write_text(json.dumps(asdict(self.state), indent=2))
        except Exception as e:
            logger.error(f"Failed to save state: {e}")

    async def _run_claude_async(self, prompt: str, timeout: int = None) -> AgentResult:
        """
        Run Claude CLI asynchronously using asyncio.create_subprocess_exec.

        This uses argument list format which is safe against command injection.
        """
        timeout = timeout or self.timeout

        # Build command as a list (safe - no shell injection possible)
        cmd_args = [
            "claude",
            "-p", prompt,
            "--dangerously-skip-permissions",
            "--model", self.model,
            "--output-format", "text"
        ]

        start_time = time.time()

        try:
            # Using create_subprocess_exec with argument list (SAFE)
            process = await asyncio.create_subprocess_exec(
                *cmd_args,
                cwd=str(self.project_path),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                return AgentResult(
                    role=AgentRole.IMPLEMENTER,
                    success=False,
                    output="",
                    error=f"Timeout after {timeout}s",
                    duration_seconds=time.time() - start_time
                )

            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=process.returncode == 0,
                output=stdout.decode(),
                error=stderr.decode() if process.returncode != 0 else None,
                duration_seconds=time.time() - start_time
            )

        except FileNotFoundError:
            return AgentResult(
                role=AgentRole.IMPLEMENTER,
                success=False,
                output="",
                error="Claude CLI not found",
                duration_seconds=0
            )

    def _parse_output(self, result: AgentResult):
        """Extract structured data from agent output."""
        for line in result.output.split('\n'):
            line = line.strip()
            if line.startswith("BRANCH_NAME:"):
                result.branch_name = line.split(":", 1)[1].strip()
            elif line.startswith("PR_NUMBER:"):
                try:
                    result.pr_number = int(line.split(":", 1)[1].strip().replace("#", ""))
                except ValueError:
                    pass

    async def run_implementer(self, task: str = None) -> AgentResult:
        prompt = f"""You are the IMPLEMENTER agent. Read TODO.md, select the highest priority task, implement it.

1. Read TODO.md for incomplete tasks
2. Create feature branch: feature/<description>
3. Implement thoroughly
4. Commit with clear messages
5. Output: BRANCH_NAME: <branch>, TASK_COMPLETED: <desc>, FILES_CHANGED: <files>

{f"Specific task: {task}" if task else ""}"""

        result = await self._run_claude_async(prompt, timeout=900)
        result.role = AgentRole.IMPLEMENTER
        self._parse_output(result)
        return result

    async def run_tester(self, branch: str = None) -> AgentResult:
        branch_info = f"on branch '{branch}'" if branch else "on current branch"
        prompt = f"""You are the TESTER agent. Test changes {branch_info}, create PR if tests pass.

1. Run test suite
2. Fix failures
3. Run linting
4. Create PR: gh pr create
5. Output: PR_NUMBER: <num>, TEST_RESULTS: <summary>"""

        result = await self._run_claude_async(prompt)
        result.role = AgentRole.TESTER
        result.branch_name = branch
        self._parse_output(result)
        return result

    async def run_reviewer(self, pr_number: int = None) -> AgentResult:
        pr_info = f"#{pr_number}" if pr_number else "most recent"
        prompt = f"""You are the REVIEWER agent. Review PR {pr_info} thoroughly.

1. gh pr view / gh pr diff
2. Check: bugs, security, performance
3. Approve or request changes
4. Output: REVIEW_DECISION: APPROVED/CHANGES_REQUESTED, KEY_FEEDBACK: <points>"""

        result = await self._run_claude_async(prompt, timeout=300)
        result.role = AgentRole.REVIEWER
        result.pr_number = pr_number
        return result

    async def run_planner(self) -> AgentResult:
        prompt = """You are the PLANNER agent. Analyze codebase, add valuable tasks to TODO.md.

1. Read current TODO.md
2. Explore codebase
3. Identify: features, optimizations, refactors, security, tests
4. Add 3-5 prioritized tasks
5. Output: TASKS_ADDED: <list>, CODEBASE_HEALTH: <assessment>"""

        result = await self._run_claude_async(prompt, timeout=300)
        result.role = AgentRole.PLANNER
        return result

    async def run_pipeline(self, parallel: bool = True) -> Dict[str, AgentResult]:
        """Run full pipeline with optional parallel execution."""
        logger.info("=" * 50)
        logger.info("Starting async pipeline")
        logger.info("=" * 50)

        results = {}

        # Stage 1: Implementer
        logger.info("\n[1/4] Implementer")
        impl = await self.run_implementer()
        results['implementer'] = impl

        if not impl.success:
            return results

        # Stage 2: Tester
        logger.info("\n[2/4] Tester")
        test = await self.run_tester(branch=impl.branch_name)
        results['tester'] = test

        if not test.success:
            return results

        # Stage 3+4: Reviewer & Planner (can run in parallel!)
        if parallel:
            logger.info("\n[3+4] Reviewer & Planner (parallel)")
            review_task = self.run_reviewer(pr_number=test.pr_number)
            plan_task = self.run_planner()
            review, plan = await asyncio.gather(review_task, plan_task)
            results['reviewer'] = review
            results['planner'] = plan
        else:
            logger.info("\n[3/4] Reviewer")
            results['reviewer'] = await self.run_reviewer(pr_number=test.pr_number)
            logger.info("\n[4/4] Planner")
            results['planner'] = await self.run_planner()

        # Update state
        self.state.last_run = datetime.now().isoformat()
        self.state.total_cycles += 1
        if all(r.success for r in results.values()):
            self.state.successful_cycles += 1
        self.state.last_branch = impl.branch_name
        self.state.last_pr = test.pr_number
        self._save_state()

        self._print_summary(results)
        return results

    def _print_summary(self, results: Dict[str, AgentResult]):
        total_time = sum(r.duration_seconds for r in results.values())
        print("\n" + "=" * 50)
        print("📊 Pipeline Summary")
        print("-" * 50)
        for name, r in results.items():
            status = "✓" if r.success else "✗"
            print(f"{status} {name.upper()}: {r.duration_seconds:.1f}s")
        print("-" * 50)
        print(f"Total: {total_time:.1f}s | Success: {sum(1 for r in results.values() if r.success)}/{len(results)}")


async def main():
    import argparse
    parser = argparse.ArgumentParser(description="Async Claude Orchestra")
    parser.add_argument("--project", "-P", required=True)
    parser.add_argument("--pipeline", action="store_true")
    parser.add_argument("--parallel", action="store_true")
    parser.add_argument("--implement", action="store_true")
    parser.add_argument("--test", action="store_true")
    parser.add_argument("--review", action="store_true")
    parser.add_argument("--plan", action="store_true")
    parser.add_argument("--branch", help="Branch for tester")
    parser.add_argument("--pr", type=int, help="PR for reviewer")
    parser.add_argument("--model", default="sonnet")

    args = parser.parse_args()
    orchestra = AsyncClaudeOrchestra(args.project, model=args.model)

    if args.pipeline:
        await orchestra.run_pipeline(parallel=args.parallel)
    elif args.implement:
        r = await orchestra.run_implementer()
        print(f"Result: {'✓' if r.success else '✗'}\n{r.output}")
    elif args.test:
        r = await orchestra.run_tester(branch=args.branch)
        print(f"Result: {'✓' if r.success else '✗'}\n{r.output}")
    elif args.review:
        r = await orchestra.run_reviewer(pr_number=args.pr)
        print(f"Result: {'✓' if r.success else '✗'}\n{r.output}")
    elif args.plan:
        r = await orchestra.run_planner()
        print(f"Result: {'✓' if r.success else '✗'}\n{r.output}")


if __name__ == "__main__":
    asyncio.run(main())
