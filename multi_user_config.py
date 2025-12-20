#!/usr/bin/env python3
"""
Multi-User Configuration - Settings for distributed task coordination

This module provides configuration for multi-user mode where multiple
users can run Claude Orchestra on the same repository without conflicts.
"""

import os
from dataclasses import dataclass, field
from typing import Optional, List
from pathlib import Path


@dataclass
class MultiUserConfig:
    """Configuration for multi-user task coordination via GitHub Issues."""

    # Core settings
    enabled: bool = False
    github_token: str = ""
    repo_owner: str = ""
    repo_name: str = ""

    # Timing settings
    heartbeat_interval: int = 300   # 5 minutes - how often to update heartbeat
    claim_timeout: int = 1800       # 30 minutes - when claims become stale

    # Sync settings
    auto_sync_todos: bool = True    # Sync TODO.md to issues on startup
    todo_files: List[str] = field(default_factory=lambda: [
        "TODO.md",
        "docs/TODO.md",
        "docs/TASKS.md"
    ])

    # Task selection - default to high priority to ensure important tasks are picked first
    prefer_priority: Optional[str] = "high"  # "highest", "high", "medium", "low"
    prefer_size: Optional[str] = None        # "small", "medium", "large"

    @classmethod
    def from_env(cls) -> 'MultiUserConfig':
        """
        Create configuration from environment variables.

        Environment variables:
            ORCHESTRA_MULTI_USER: "true" to enable
            GITHUB_TOKEN: GitHub personal access token
            GITHUB_REPO: Repository in "owner/name" format
            ORCHESTRA_HEARTBEAT_INTERVAL: Heartbeat interval in seconds
            ORCHESTRA_CLAIM_TIMEOUT: Claim timeout in seconds
            ORCHESTRA_AUTO_SYNC: "true" to auto-sync TODOs
        """
        repo = os.getenv("GITHUB_REPO", "")
        if "/" in repo:
            owner, name = repo.split("/", 1)
        else:
            owner = os.getenv("GITHUB_REPO_OWNER", "")
            name = os.getenv("GITHUB_REPO_NAME", "")

        return cls(
            enabled=os.getenv("ORCHESTRA_MULTI_USER", "false").lower() == "true",
            github_token=os.getenv("GITHUB_TOKEN", ""),
            repo_owner=owner,
            repo_name=name,
            heartbeat_interval=int(os.getenv("ORCHESTRA_HEARTBEAT_INTERVAL", "300")),
            claim_timeout=int(os.getenv("ORCHESTRA_CLAIM_TIMEOUT", "1800")),
            auto_sync_todos=os.getenv("ORCHESTRA_AUTO_SYNC", "true").lower() == "true",
            prefer_priority=os.getenv("ORCHESTRA_PREFER_PRIORITY", "high"),
            prefer_size=os.getenv("ORCHESTRA_PREFER_SIZE"),
        )

    @classmethod
    def from_file(cls, config_path: Path) -> 'MultiUserConfig':
        """Load configuration from a JSON or YAML file."""
        import json

        if not config_path.exists():
            return cls()

        content = config_path.read_text()

        if config_path.suffix in ('.yaml', '.yml'):
            try:
                import yaml
                data = yaml.safe_load(content)
            except ImportError:
                raise ImportError("PyYAML required for YAML config files")
        else:
            data = json.loads(content)

        multi_user = data.get("multi_user", {})
        return cls(
            enabled=multi_user.get("enabled", False),
            github_token=multi_user.get("github_token", os.getenv("GITHUB_TOKEN", "")),
            repo_owner=multi_user.get("repo_owner", ""),
            repo_name=multi_user.get("repo_name", ""),
            heartbeat_interval=multi_user.get("heartbeat_interval", 300),
            claim_timeout=multi_user.get("claim_timeout", 1800),
            auto_sync_todos=multi_user.get("auto_sync_todos", True),
            todo_files=multi_user.get("todo_files", [
                "TODO.md", "docs/TODO.md", "docs/TASKS.md"
            ]),
            prefer_priority=multi_user.get("prefer_priority"),
            prefer_size=multi_user.get("prefer_size"),
        )

    def validate(self) -> List[str]:
        """
        Validate the configuration.

        Returns:
            List of validation error messages (empty if valid)
        """
        errors = []

        if self.enabled:
            if not self.github_token:
                errors.append("GITHUB_TOKEN is required for multi-user mode")
            if not self.repo_owner:
                errors.append("Repository owner is required for multi-user mode")
            if not self.repo_name:
                errors.append("Repository name is required for multi-user mode")

            if self.heartbeat_interval < 60:
                errors.append("Heartbeat interval must be at least 60 seconds")

            if self.claim_timeout < self.heartbeat_interval * 2:
                errors.append("Claim timeout should be at least 2x heartbeat interval")

        return errors

    def is_valid(self) -> bool:
        """Check if configuration is valid."""
        return len(self.validate()) == 0

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "enabled": self.enabled,
            "repo_owner": self.repo_owner,
            "repo_name": self.repo_name,
            "heartbeat_interval": self.heartbeat_interval,
            "claim_timeout": self.claim_timeout,
            "auto_sync_todos": self.auto_sync_todos,
            "todo_files": self.todo_files,
            "prefer_priority": self.prefer_priority,
            "prefer_size": self.prefer_size,
            # Never serialize the token
        }

    def print_summary(self):
        """Print configuration summary."""
        print("\n" + "=" * 60)
        print("Multi-User Configuration")
        print("=" * 60)
        print(f"  Enabled:            {self.enabled}")
        if self.enabled:
            print(f"  Repository:         {self.repo_owner}/{self.repo_name}")
            print(f"  GitHub Token:       {'*' * 8}...{self.github_token[-4:] if self.github_token else 'NOT SET'}")
            print(f"  Heartbeat:          {self.heartbeat_interval}s ({self.heartbeat_interval // 60} min)")
            print(f"  Claim Timeout:      {self.claim_timeout}s ({self.claim_timeout // 60} min)")
            print(f"  Auto-sync TODOs:    {self.auto_sync_todos}")
            if self.prefer_priority:
                print(f"  Prefer Priority:    {self.prefer_priority}")
            if self.prefer_size:
                print(f"  Prefer Size:        {self.prefer_size}")
        print("=" * 60 + "\n")


def add_multi_user_args(parser) -> None:
    """Add multi-user CLI arguments to an argument parser."""
    group = parser.add_argument_group('Multi-User Options')

    group.add_argument(
        '--multi-user',
        action='store_true',
        help='Enable multi-user mode with GitHub Issues coordination'
    )

    group.add_argument(
        '--github-token',
        type=str,
        help='GitHub token (or set GITHUB_TOKEN env var)'
    )

    group.add_argument(
        '--repo',
        type=str,
        help='GitHub repository in owner/name format'
    )

    group.add_argument(
        '--heartbeat-interval',
        type=int,
        default=300,
        help='Heartbeat interval in seconds (default: 300)'
    )

    group.add_argument(
        '--claim-timeout',
        type=int,
        default=1800,
        help='Claim timeout in seconds (default: 1800)'
    )

    group.add_argument(
        '--no-sync',
        action='store_true',
        help='Disable auto-sync of TODO.md to GitHub Issues'
    )

    group.add_argument(
        '--prefer-priority',
        choices=['high', 'medium', 'low'],
        help='Prefer tasks of this priority'
    )

    group.add_argument(
        '--prefer-size',
        choices=['small', 'medium', 'large'],
        help='Prefer tasks of this size'
    )


def config_from_args(args) -> MultiUserConfig:
    """Create MultiUserConfig from parsed CLI arguments."""
    # Start with environment-based config
    config = MultiUserConfig.from_env()

    # Override with CLI args
    if hasattr(args, 'multi_user') and args.multi_user:
        config.enabled = True

    if hasattr(args, 'github_token') and args.github_token:
        config.github_token = args.github_token

    if hasattr(args, 'repo') and args.repo:
        if '/' in args.repo:
            config.repo_owner, config.repo_name = args.repo.split('/', 1)

    if hasattr(args, 'heartbeat_interval'):
        config.heartbeat_interval = args.heartbeat_interval

    if hasattr(args, 'claim_timeout'):
        config.claim_timeout = args.claim_timeout

    if hasattr(args, 'no_sync') and args.no_sync:
        config.auto_sync_todos = False

    if hasattr(args, 'prefer_priority') and args.prefer_priority:
        config.prefer_priority = args.prefer_priority

    if hasattr(args, 'prefer_size') and args.prefer_size:
        config.prefer_size = args.prefer_size

    return config


# Example config file template
EXAMPLE_CONFIG = """
# Multi-User Configuration Example
# Save as .orchestra_config.json or .orchestra_config.yaml

{
  "multi_user": {
    "enabled": true,
    "repo_owner": "your-org",
    "repo_name": "your-repo",
    "heartbeat_interval": 300,
    "claim_timeout": 1800,
    "auto_sync_todos": true,
    "todo_files": [
      "TODO.md",
      "docs/TODO.md"
    ],
    "prefer_priority": "high",
    "prefer_size": null
  }
}
"""
