#!/usr/bin/env python3
"""
Orchestra Multi-User - ClaudeOrchestra with distributed task coordination

This module extends ClaudeOrchestra to use GitHub Issues for task coordination,
enabling multiple users to work on the same repository without conflicts.

Usage:
    # Via environment variables
    export ORCHESTRA_MULTI_USER=true
    export GITHUB_TOKEN=ghp_xxx
    export GITHUB_REPO=owner/repo
    python orchestra_multi_user.py --project /path/to/project

    # Via CLI arguments
    python orchestra_multi_user.py --project /path/to/project \
        --multi-user --repo owner/repo
"""

import asyncio
import logging
import signal
import sys
from pathlib import Path
from typing import Optional
from dataclasses import dataclass

from claude_orchestra import ClaudeOrchestra, AgentResult, AgentRole
from task_coordinator import TaskCoordinator, ClaimResult, TaskStatus
from multi_user_config import MultiUserConfig, add_multi_user_args, config_from_args
from instance_manager import InstanceManager
from queue_manager import get_queue

logger = logging.getLogger(__name__)


@dataclass
class MultiUserAgentResult(AgentResult):
    """Extended AgentResult with multi-user tracking."""
    issue_number: Optional[int] = None
    claim_info: Optional[dict] = None


class MultiUserOrchestra:
    """
    ClaudeOrchestra wrapper with multi-user task coordination.

    This class wraps the standard ClaudeOrchestra and adds:
    - Task claiming via GitHub Issues
    - Heartbeat updates while working
    - Automatic release of stale claims
    - Progress tracking visible to all users
    """

    def __init__(
        self,
        project_path: str,
        config: Optional[MultiUserConfig] = None,
        **orchestra_kwargs
    ):
        self.project_path = Path(project_path).resolve()
        self.config = config or MultiUserConfig.from_env()

        # Validate config
        if self.config.enabled:
            errors = self.config.validate()
            if errors:
                raise ValueError(f"Invalid multi-user config: {'; '.join(errors)}")

        # Initialize instance manager for local isolation
        self.instance_manager = InstanceManager(str(self.project_path))

        # Initialize the base orchestra
        self.orchestra = ClaudeOrchestra(
            project_path=str(self.project_path),
            **orchestra_kwargs
        )

        # Task coordinator (initialized async)
        self.coordinator: Optional[TaskCoordinator] = None
        self._current_claim: Optional[ClaimResult] = None
        self._heartbeat_task: Optional[asyncio.Task] = None

        logger.info(f"MultiUserOrchestra initialized")
        logger.info(f"  Project: {self.project_path}")
        logger.info(f"  Multi-user: {self.config.enabled}")
        if self.config.enabled:
            logger.info(f"  Repo: {self.config.repo_owner}/{self.config.repo_name}")

    async def setup(self) -> None:
        """Initialize async components."""
        if not self.config.enabled:
            logger.info("Multi-user mode disabled, using local task selection")
            return

        # Create coordinator
        self.coordinator = TaskCoordinator(
            repo_owner=self.config.repo_owner,
            repo_name=self.config.repo_name,
            github_token=self.config.github_token,
            project_path=str(self.project_path),
            heartbeat_interval=self.config.heartbeat_interval,
            claim_timeout=self.config.claim_timeout
        )

        await self.coordinator.setup()

        # Sync TODOs if enabled
        if self.config.auto_sync_todos:
            logger.info("Syncing TODO.md to GitHub Issues...")
            result = await self.coordinator.sync_todos_to_issues(
                todo_files=self.config.todo_files
            )
            logger.info(f"Sync complete: {result.created} created, {result.updated} updated")

        # Check for stale claims
        stale_count = await self.coordinator.reclaim_stale_tasks()
        if stale_count > 0:
            logger.info(f"Released {stale_count} stale claims")

        # Register instance
        self.instance_manager.register_instance()

        logger.info("Multi-user setup complete")
        logger.info(f"  Agent ID: {self.coordinator.agent.agent_id}")

    async def cleanup(self) -> None:
        """Cleanup resources."""
        # Stop heartbeat
        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        # Release any active claims
        if self._current_claim and self.coordinator:
            await self.coordinator.release_claim(
                self._current_claim.issue_number,
                reason="orchestra_shutdown"
            )

        # Close coordinator
        if self.coordinator:
            await self.coordinator.close()

        # Unregister instance
        self.instance_manager.unregister_instance()

    async def run_implementer(
        self,
        task_description: Optional[str] = None
    ) -> MultiUserAgentResult:
        """
        Run the Implementer agent with coordinated task claiming.

        In multi-user mode:
        1. Claims a task from GitHub Issues
        2. Starts heartbeat updates
        3. Runs the implementer with the claimed task
        4. Updates progress/completion status
        """
        if not self.config.enabled or not self.coordinator:
            # Fall back to standard orchestra
            result = self.orchestra.run_implementer(task_description)
            return MultiUserAgentResult(
                role=result.role,
                success=result.success,
                output=result.output,
                error=result.error,
                pr_number=result.pr_number,
                branch_name=result.branch_name,
                review_decision=result.review_decision,
                review_feedback=result.review_feedback
            )

        # Claim a task
        claim = await self.coordinator.claim_next_available(
            priority=self.config.prefer_priority,
            size=self.config.prefer_size
        )

        if not claim.success:
            logger.info(f"No tasks available to claim: {claim.reason}")
            return MultiUserAgentResult(
                role=AgentRole.IMPLEMENTER,
                success=False,
                output="",
                error=f"No tasks available: {claim.reason}"
            )

        self._current_claim = claim
        logger.info(f"Claimed issue #{claim.issue_number}: {claim.task.title}")

        # Start heartbeat
        self._heartbeat_task = await self.coordinator.start_heartbeat_loop()

        # Update status to in-progress
        await self.coordinator.update_progress(
            claim.issue_number,
            status="in-progress",
            progress_note="Starting implementation"
        )

        try:
            # Run implementer with the claimed task
            # Override branch naming to use our format
            task_prompt = f"""MULTI-USER MODE - CLAIMED TASK FROM GITHUB ISSUE #{claim.issue_number}

=== YOUR ASSIGNED TASK ===
Title: {claim.task.title}

Description:
{claim.task.description}

=== CRITICAL INSTRUCTIONS ===
1. DO NOT look at TODO.md or docs/TODO.md for task selection - your task is defined above
2. DO NOT modify any TODO files - task tracking is via GitHub Issues
3. Create branch: {claim.branch_name}
4. Implement the task described above

=== GITHUB ISSUE LINKING (MANDATORY) ===
This task is linked to GitHub Issue #{claim.issue_number}.
When the TESTER agent creates a PR, it MUST include "Fixes #{claim.issue_number}" in the PR body.
This will automatically close the issue when the PR is merged.

This task was claimed from the shared GitHub Issues task queue.
Other agents may be working on other issues simultaneously.
Focus only on implementing the task described above.
"""

            result = self.orchestra.run_implementer(task_description=task_prompt)

            # Update progress
            if result.success:
                progress_note = f"Implementation complete"
                if result.branch_name:
                    progress_note += f" on {result.branch_name}"

                await self.coordinator.update_progress(
                    claim.issue_number,
                    progress_note=progress_note
                )

            return MultiUserAgentResult(
                role=result.role,
                success=result.success,
                output=result.output,
                error=result.error,
                pr_number=result.pr_number,
                branch_name=result.branch_name or claim.branch_name,
                review_decision=result.review_decision,
                review_feedback=result.review_feedback,
                issue_number=claim.issue_number,
                claim_info=claim.claim_info.__dict__ if claim.claim_info else None
            )

        except Exception as e:
            logger.error(f"Error during implementation: {e}")
            await self.coordinator.mark_blocked(
                claim.issue_number,
                reason=str(e)
            )
            raise

    async def run_tester(
        self,
        branch_name: Optional[str] = None
    ) -> MultiUserAgentResult:
        """Run tester with multi-user tracking."""
        # Use claim's branch if available
        if not branch_name and self._current_claim:
            branch_name = self._current_claim.branch_name

        # Pass issue number so PR description includes "Fixes #<issue>"
        issue_number = self._current_claim.issue_number if self._current_claim else None
        result = self.orchestra.run_tester(branch_name, issue_number=issue_number)

        # Update issue with PR if created
        if result.pr_number and self._current_claim and self.coordinator:
            await self.coordinator.mark_pr_created(
                self._current_claim.issue_number,
                result.pr_number
            )

        return MultiUserAgentResult(
            role=result.role,
            success=result.success,
            output=result.output,
            error=result.error,
            pr_number=result.pr_number,
            branch_name=result.branch_name,
            review_decision=result.review_decision,
            review_feedback=result.review_feedback,
            issue_number=self._current_claim.issue_number if self._current_claim else None
        )

    async def run_reviewer(
        self,
        pr_number: Optional[int] = None
    ) -> MultiUserAgentResult:
        """Run reviewer with multi-user tracking."""
        result = self.orchestra.run_reviewer(pr_number)

        return MultiUserAgentResult(
            role=result.role,
            success=result.success,
            output=result.output,
            error=result.error,
            pr_number=result.pr_number,
            branch_name=result.branch_name,
            review_decision=result.review_decision,
            review_feedback=result.review_feedback,
            issue_number=self._current_claim.issue_number if self._current_claim else None
        )

    async def run_cycle(self, task_override: Optional[str] = None) -> dict:
        """
        Run a complete development cycle with multi-user coordination.

        Args:
            task_override: Optional task description from queue that takes priority

        Returns a dict with results from each agent stage.
        """
        results = {}

        # Setup if needed
        if self.config.enabled and not self.coordinator:
            await self.setup()

        # Implementer
        logger.info("=" * 60)
        logger.info("STAGE 1: IMPLEMENTER")
        logger.info("=" * 60)

        impl_result = await self.run_implementer(task_description=task_override)
        results['implementer'] = impl_result

        if not impl_result.success:
            logger.warning("Implementer failed, ending cycle")
            return results

        # Tester
        logger.info("=" * 60)
        logger.info("STAGE 2: TESTER")
        logger.info("=" * 60)

        test_result = await self.run_tester(impl_result.branch_name)
        results['tester'] = test_result

        if not test_result.success or not test_result.pr_number:
            logger.warning("Tester failed or no PR created")
            return results

        # Reviewer
        logger.info("=" * 60)
        logger.info("STAGE 3: REVIEWER")
        logger.info("=" * 60)

        review_result = await self.run_reviewer(test_result.pr_number)
        results['reviewer'] = review_result

        # Handle review decision
        if review_result.review_decision == "APPROVED":
            logger.info("PR approved!")
            if self._current_claim and self.coordinator:
                await self.coordinator.complete_task(
                    self._current_claim.issue_number,
                    pr_number=test_result.pr_number,
                    summary=f"Implemented: {self._current_claim.task.title}"
                )
            self._current_claim = None

        elif review_result.review_decision == "CHANGES_REQUESTED":
            logger.info("Changes requested, will need another cycle")
            # Keep the claim active for fixes

        return results

    async def run_continuous(
        self,
        max_cycles: int = 10,
        max_hours: Optional[float] = None
    ) -> list:
        """
        Run continuous cycles until no tasks remain or limits reached.

        Args:
            max_cycles: Maximum number of cycles to run
            max_hours: Maximum hours to run (None for no limit)

        Returns:
            List of cycle results
        """
        import time
        start_time = time.time()
        all_results = []

        await self.setup()

        # Initialize queue manager for checking dashboard messages
        queue = get_queue()

        for cycle_num in range(1, max_cycles + 1):
            # Check time limit
            if max_hours:
                elapsed_hours = (time.time() - start_time) / 3600
                if elapsed_hours >= max_hours:
                    logger.info(f"Time limit reached ({max_hours}h)")
                    break

            logger.info("=" * 60)
            logger.info(f"CYCLE {cycle_num}/{max_cycles}")
            logger.info("=" * 60)

            # Check for queued messages from dashboard
            queued_message = queue.get_next_pending()
            if queued_message:
                logger.info(f"[QUEUE] Found queued message: {queued_message['message'][:60]}...")
                queue.claim_message(queued_message["id"])
                # Use queued message as the task description
                task_override = queued_message["message"]
            else:
                task_override = None

            try:
                cycle_result = await self.run_cycle(task_override=task_override)
                all_results.append({
                    'cycle': cycle_num,
                    'results': cycle_result,
                    'success': all(r.success for r in cycle_result.values() if r)
                })

                # Mark queued message as completed
                if queued_message:
                    success = cycle_result.get('implementer') and cycle_result['implementer'].success
                    queue.complete_message(
                        queued_message["id"],
                        success=success,
                        result=f"Cycle {cycle_num}: {'completed' if success else 'failed'}"
                    )

                # If no task was claimed, we're done
                if not cycle_result.get('implementer') or not cycle_result['implementer'].success:
                    if cycle_result.get('implementer') and 'No tasks available' in str(cycle_result['implementer'].error):
                        logger.info("No more tasks available, ending continuous run")
                        break

            except Exception as e:
                logger.error(f"Cycle {cycle_num} failed: {e}")
                all_results.append({
                    'cycle': cycle_num,
                    'error': str(e),
                    'success': False
                })
                # Mark queued message as failed if there was one
                if queued_message:
                    queue.complete_message(queued_message["id"], success=False, result=str(e))

        await self.cleanup()
        return all_results


async def main():
    """CLI entry point."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Claude Orchestra with Multi-User Coordination"
    )

    parser.add_argument(
        '--project', '-p',
        type=str,
        required=True,
        help='Path to the project directory'
    )

    parser.add_argument(
        '--cycle',
        action='store_true',
        help='Run a complete development cycle'
    )

    parser.add_argument(
        '--continuous',
        action='store_true',
        help='Run continuous cycles until no tasks remain'
    )

    parser.add_argument(
        '--max-cycles',
        type=int,
        default=10,
        help='Maximum cycles for continuous mode'
    )

    parser.add_argument(
        '--max-hours',
        type=float,
        help='Maximum hours for continuous mode'
    )

    parser.add_argument(
        '--implementer',
        action='store_true',
        help='Run only the implementer stage'
    )

    parser.add_argument(
        '--task',
        type=str,
        help='Specific task to implement'
    )

    parser.add_argument(
        '--timeout',
        type=int,
        default=1800,
        help='Timeout per agent in seconds'
    )

    parser.add_argument(
        '--model',
        type=str,
        default='sonnet',
        choices=['sonnet', 'opus', 'haiku'],
        help='Claude model to use'
    )

    parser.add_argument(
        '--task-mode',
        type=str,
        default='normal',
        choices=['small', 'normal', 'large'],
        help='Task size preference'
    )

    parser.add_argument(
        '--guidance',
        type=str,
        help='Additional guidance for the agents'
    )

    parser.add_argument(
        '--task-queue',
        type=str,
        help='JSON string of task queue'
    )

    parser.add_argument(
        '--use-subagents',
        action='store_true',
        help='Enable sub-agent spawning'
    )

    # Add multi-user arguments
    add_multi_user_args(parser)

    args = parser.parse_args()

    # Build config
    config = config_from_args(args)

    if config.enabled:
        config.print_summary()

    # Create orchestra
    orchestra = MultiUserOrchestra(
        project_path=args.project,
        config=config,
        timeout=args.timeout,
        model=args.model,
        task_mode=args.task_mode
    )

    # Setup signal handlers
    def signal_handler(sig, frame):
        logger.info("Interrupt received, cleaning up...")
        asyncio.create_task(orchestra.cleanup())
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        if args.continuous:
            results = await orchestra.run_continuous(
                max_cycles=args.max_cycles,
                max_hours=args.max_hours
            )
            print(f"\nCompleted {len(results)} cycles")

        elif args.cycle:
            await orchestra.setup()
            results = await orchestra.run_cycle()
            print(f"\nCycle complete: {len([r for r in results.values() if r and r.success])}/{len(results)} stages succeeded")

        elif args.implementer:
            await orchestra.setup()
            result = await orchestra.run_implementer(args.task)
            print(f"\nImplementer {'succeeded' if result.success else 'failed'}")
            if result.issue_number:
                print(f"Issue: #{result.issue_number}")
            if result.branch_name:
                print(f"Branch: {result.branch_name}")

        else:
            parser.print_help()
            return 1

    finally:
        await orchestra.cleanup()

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
