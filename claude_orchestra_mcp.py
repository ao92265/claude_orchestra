#!/usr/bin/env python3
"""
Claude Orchestra with MCP Server Support

SECURITY: This script uses Python's asyncio.create_subprocess_exec() with explicit
argument lists. This is the SAFE way to run subprocesses - it does NOT use shell=True
and is NOT vulnerable to command injection. See Python docs:
https://docs.python.org/3/library/asyncio-subprocess.html

Usage:
    python claude_orchestra_mcp.py --project /path/to/project --pipeline --with-ui-testing
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Dict, List
from datetime import datetime

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class AgentRole(Enum):
    IMPLEMENTER = "implementer"
    TESTER = "tester"
    UI_TESTER = "ui_tester"
    REVIEWER = "reviewer"
    PLANNER = "planner"


@dataclass
class AgentResult:
    role: AgentRole
    success: bool
    output: str
    error: Optional[str] = None
    pr_number: Optional[int] = None
    branch_name: Optional[str] = None
    duration_seconds: float = 0.0
    screenshots: List[str] = None

    def __post_init__(self):
        if self.screenshots is None:
            self.screenshots = []


class MCPClaudeOrchestra:
    """Orchestra with MCP server integration for UI testing via Playwright."""

    def __init__(self, project_path: str, timeout: int = 600, model: str = "sonnet"):
        self.project_path = Path(project_path).resolve()
        self.timeout = timeout
        self.model = model
        if not self.project_path.exists():
            raise ValueError(f"Project path does not exist: {self.project_path}")

    async def _run_claude(self, prompt: str, timeout: int = None, mcp_servers: List[str] = None) -> AgentResult:
        """Run Claude CLI. Uses argument list format which is safe against injection."""
        timeout = timeout or self.timeout
        cmd_args = ["claude", "-p", prompt, "--dangerously-skip-permissions", "--model", self.model, "--output-format", "text"]
        for server in (mcp_servers or []):
            cmd_args.extend(["--mcp-server", server])

        start_time = time.time()
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd_args, cwd=str(self.project_path),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
            try:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                return AgentResult(role=AgentRole.IMPLEMENTER, success=False, output="", 
                                   error=f"Timeout after {timeout}s", duration_seconds=time.time() - start_time)
            return AgentResult(role=AgentRole.IMPLEMENTER, success=proc.returncode == 0,
                               output=stdout.decode(), error=stderr.decode() if proc.returncode != 0 else None,
                               duration_seconds=time.time() - start_time)
        except FileNotFoundError:
            return AgentResult(role=AgentRole.IMPLEMENTER, success=False, output="", error="Claude CLI not found")

    async def run_ui_tester(self, branch: str = None, app_url: str = "http://localhost:3000") -> AgentResult:
        """UI Tester using Playwright MCP for visual testing."""
        prompt = f"""You are the UI_TESTER agent with Playwright capabilities.
Test the application at {app_url}:
1. browser_navigate to {app_url}
2. browser_snapshot for accessibility tree
3. Test user flows: navigation, forms, buttons, errors
4. browser_take_screenshot for important states
5. Report issues

Output: UI_TEST_PASSED: true/false, SCREENSHOTS: <list>, ISSUES_FOUND: <issues>
Branch: {branch or 'current'}"""
        result = await self._run_claude(prompt, timeout=900, mcp_servers=["playwright"])
        result.role = AgentRole.UI_TESTER
        return result

    async def run_implementer(self) -> AgentResult:
        prompt = "You are IMPLEMENTER. Read TODO.md, implement highest priority task, create branch, commit. Output: BRANCH_NAME: <branch>"
        result = await self._run_claude(prompt, timeout=900)
        result.role = AgentRole.IMPLEMENTER
        self._parse_output(result)
        return result

    async def run_tester(self, branch: str = None) -> AgentResult:
        prompt = f"You are TESTER. Run tests, fix failures, create PR. Branch: {branch}. Output: PR_NUMBER: <num>"
        result = await self._run_claude(prompt, timeout=600)
        result.role = AgentRole.TESTER
        self._parse_output(result)
        return result

    async def run_reviewer(self, pr_number: int = None) -> AgentResult:
        prompt = f"You are REVIEWER. Review PR #{pr_number}. Approve or request changes."
        result = await self._run_claude(prompt, timeout=300)
        result.role = AgentRole.REVIEWER
        return result

    async def run_planner(self) -> AgentResult:
        prompt = "You are PLANNER. Analyze codebase, add 3-5 tasks to TODO.md."
        result = await self._run_claude(prompt, timeout=300)
        result.role = AgentRole.PLANNER
        return result

    def _parse_output(self, result: AgentResult):
        for line in result.output.split('\n'):
            if line.strip().startswith("BRANCH_NAME:"):
                result.branch_name = line.split(":", 1)[1].strip()
            elif line.strip().startswith("PR_NUMBER:"):
                try:
                    result.pr_number = int(line.split(":", 1)[1].strip().replace("#", ""))
                except ValueError:
                    pass

    async def run_pipeline_with_ui(self, app_url: str = "http://localhost:3000") -> Dict[str, AgentResult]:
        """Full pipeline with UI testing."""
        logger.info("Starting pipeline with UI testing")
        results = {}

        impl = await self.run_implementer()
        results['implementer'] = impl
        if not impl.success:
            return results

        # Unit tests + UI tests in parallel
        unit_task = self.run_tester(branch=impl.branch_name)
        ui_task = self.run_ui_tester(branch=impl.branch_name, app_url=app_url)
        unit_result, ui_result = await asyncio.gather(unit_task, ui_task)
        results['tester'] = unit_result
        results['ui_tester'] = ui_result

        if not unit_result.success or not ui_result.success:
            return results

        # Reviewer + Planner in parallel
        review, plan = await asyncio.gather(
            self.run_reviewer(pr_number=unit_result.pr_number),
            self.run_planner()
        )
        results['reviewer'] = review
        results['planner'] = plan

        self._print_summary(results)
        return results

    def _print_summary(self, results: Dict[str, AgentResult]):
        print("\n" + "=" * 50)
        print("Pipeline Summary")
        for name, r in results.items():
            print(f"{'✓' if r.success else '✗'} {name.upper()}: {r.duration_seconds:.1f}s")


async def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", "-P", required=True)
    parser.add_argument("--pipeline", action="store_true")
    parser.add_argument("--with-ui-testing", action="store_true")
    parser.add_argument("--app-url", default="http://localhost:3000")
    parser.add_argument("--ui-test-only", action="store_true")
    args = parser.parse_args()

    orchestra = MCPClaudeOrchestra(project_path=args.project)
    if args.ui_test_only:
        result = await orchestra.run_ui_tester(app_url=args.app_url)
        print(f"UI Test: {'✓' if result.success else '✗'}\n{result.output}")
    elif args.pipeline and args.with_ui_testing:
        await orchestra.run_pipeline_with_ui(app_url=args.app_url)

if __name__ == "__main__":
    asyncio.run(main())
