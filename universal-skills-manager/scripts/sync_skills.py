#!/usr/bin/env python3
"""
Skill Sync Status Reporter

A read-only diagnostic tool that detects installed AI tools, inventories skills
across them, compares content via hashes, and outputs a sync status report.

This script never creates, modifies, or deletes any files. All write operations
(copy, overwrite, deploy) are performed by the calling agent with explicit user
approval -- never by this script.

Usage:
    python3 sync_skills.py
    python3 sync_skills.py --skill code-review
    python3 sync_skills.py --project-dir /path/to/project
    python3 sync_skills.py --json
"""

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

VERSION = "1.0.0"

# ============================================================================
# Tool Registry
# ============================================================================

TOOLS = [
    {
        "id": "gemini-cli",
        "name": "Gemini CLI",
        "user_path": "~/.gemini/skills",
        "project_path": ".gemini/skills",
    },
    {
        "id": "anti-gravity",
        "name": "Google Anti-Gravity",
        "user_path": "~/.gemini/antigravity/skills",
        "project_path": ".antigravity/extensions",
    },
    {
        "id": "opencode",
        "name": "OpenCode",
        "user_path": "~/.config/opencode/skills",
        "project_path": ".opencode/skills",
    },
    {
        "id": "openclaw",
        "name": "OpenClaw",
        "user_path": "~/.openclaw/workspace/skills",
        "project_path": ".openclaw/skills",
    },
    {
        "id": "claude-code",
        "name": "Claude Code",
        "user_path": "~/.claude/skills",
        "project_path": ".claude/skills",
    },
    {
        "id": "openai-codex",
        "name": "OpenAI Codex",
        "user_path": "~/.agents/skills",
        "project_path": ".agents/skills",
    },
    {
        "id": "goose",
        "name": "block/goose",
        "user_path": "~/.config/goose/skills",
        "project_path": ".goose/agents",
    },
    {
        "id": "roo-code",
        "name": "Roo Code",
        "user_path": "~/.roo/skills",
        "project_path": ".roo/skills",
    },
    {
        "id": "cursor",
        "name": "Cursor",
        "user_path": "~/.cursor/skills",
        "project_path": ".cursor/skills",
    },
    {
        "id": "cline",
        "name": "Cline",
        "user_path": "~/.cline/skills",
        "project_path": ".cline/skills",
    },
]


# ============================================================================
# Hashing Utilities
# ============================================================================

def file_hash(file_path: Path) -> str:
    """Calculate MD5 hash of a file for comparison."""
    hasher = hashlib.md5()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(8192), b''):
            hasher.update(chunk)
    return hasher.hexdigest()


def directory_hash(directory: Path) -> str:
    """
    Compute a composite hash for an entire directory.

    Hashes each file's relative path + content, then hashes the sorted list
    so that identical directory trees always produce the same result.
    Unreadable files are silently skipped.
    """
    file_hashes = []
    for file_path in sorted(directory.rglob('*')):
        if file_path.is_file() and not file_path.is_symlink():
            rel = str(file_path.relative_to(directory))
            try:
                fh = file_hash(file_path)
            except OSError:
                continue
            file_hashes.append(f"{rel}:{fh}")
    combined = hashlib.md5('\n'.join(file_hashes).encode()).hexdigest()
    return combined


# ============================================================================
# Frontmatter Parsing
# ============================================================================

def parse_simple_yaml(yaml_str: str) -> dict:
    """
    Parse simple key: value YAML (no nested objects, no lists).
    Sufficient for extracting name/version from SKILL.md frontmatter.
    """
    result = {}
    for line in yaml_str.strip().split('\n'):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        if ':' in line:
            key, value = line.split(':', 1)
            result[key.strip()] = value.strip().strip('"').strip("'")
    return result


def extract_frontmatter(skill_md: Path) -> dict:
    """Extract YAML frontmatter fields from a SKILL.md file."""
    try:
        content = skill_md.read_text(encoding='utf-8')
    except (OSError, UnicodeDecodeError):
        return {}
    if not content.startswith('---'):
        return {}
    parts = content.split('---', 2)
    if len(parts) < 3:
        return {}
    try:
        return parse_simple_yaml(parts[1])
    except (ValueError, AttributeError):
        return {}


# ============================================================================
# Tool Detection
# ============================================================================

def resolve_tool_path(raw_path: str, home: Optional[Path] = None) -> Path:
    """
    Resolve a tool path, replacing ~ with the provided home directory.

    Accepting home as a parameter makes this testable without touching the
    real filesystem.
    """
    if home is not None:
        return home / raw_path.removeprefix("~/")
    return Path(raw_path).expanduser()


def detect_tools(
    home: Optional[Path] = None,
    project_dir: Optional[Path] = None,
) -> list[dict]:
    """
    Detect which AI tools are installed by probing for their skills directories.

    Returns a list of dicts with keys: id, name, scope, path (resolved Path).
    A tool is "detected" if its skills directory exists.
    """
    detected = []
    for tool in TOOLS:
        user_dir = resolve_tool_path(tool["user_path"], home)
        if user_dir.is_dir():
            detected.append({
                "id": tool["id"],
                "name": tool["name"],
                "scope": "user",
                "path": user_dir,
            })
        if project_dir is not None:
            proj_dir = project_dir / tool["project_path"]
            if proj_dir.is_dir():
                detected.append({
                    "id": tool["id"],
                    "name": tool["name"],
                    "scope": "project",
                    "path": proj_dir,
                })
    return detected


# ============================================================================
# Skill Inventory
# ============================================================================

def latest_mtime(directory: Path) -> Optional[float]:
    """Return the most recent mtime across all files in directory."""
    newest = None
    for file_path in directory.rglob('*'):
        if file_path.is_file() and not file_path.is_symlink():
            try:
                mt = file_path.stat().st_mtime
            except OSError:
                continue
            if newest is None or mt > newest:
                newest = mt
    return newest


def inventory_tool(tool_entry: dict) -> dict[str, dict]:
    """
    List all skills installed under a single tool's skills directory.

    Returns {skill_name: {path, hash, mtime, mtime_iso, version, name}}.
    A subdirectory counts as a skill if it contains a SKILL.md file.
    """
    skills = {}
    skills_dir = tool_entry["path"]
    if not skills_dir.is_dir():
        return skills

    for child in sorted(skills_dir.iterdir()):
        if not child.is_dir() or child.is_symlink():
            continue
        skill_md = child / "SKILL.md"
        if not skill_md.exists():
            continue

        skill_name = child.name
        fm = extract_frontmatter(skill_md)
        mtime = latest_mtime(child)
        mtime_iso = (
            datetime.fromtimestamp(mtime, tz=timezone.utc).isoformat()
            if mtime else None
        )

        skills[skill_name] = {
            "path": str(child),
            "hash": directory_hash(child),
            "mtime": mtime,
            "mtime_iso": mtime_iso,
            "version": fm.get("version"),
            "name": fm.get("name", skill_name),
        }
    return skills


def build_inventory(detected_tools: list[dict]) -> dict:
    """
    Build a unified inventory of all skills across all detected tools.

    Returns:
        {
            "skill-name": {
                "tool-id:scope": { path, hash, mtime, mtime_iso, version, name, tool_name },
                ...
            },
            ...
        }
    """
    inventory = {}
    for tool_entry in detected_tools:
        tool_key = f"{tool_entry['id']}:{tool_entry['scope']}"
        skills = inventory_tool(tool_entry)
        for skill_name, info in skills.items():
            if skill_name not in inventory:
                inventory[skill_name] = {}
            info["tool_name"] = tool_entry["name"]
            info["tool_id"] = tool_entry["id"]
            info["scope"] = tool_entry["scope"]
            inventory[skill_name][tool_key] = info
    return inventory


# ============================================================================
# Comparison
# ============================================================================

def compare_inventory(inventory: dict) -> list[dict]:
    """
    Compare each skill across its installed locations.

    Returns a list of skill status dicts:
        {
            "skill": skill name,
            "status": "in_sync" | "out_of_sync" | "single",
            "locations": [ { tool_key, tool_name, scope, path, hash, mtime_iso, version, is_newest } ],
            "newest_tool_key": tool_key of the newest copy (by mtime),
        }
    """
    results = []
    for skill_name in sorted(inventory.keys()):
        locations_map = inventory[skill_name]
        locations = []
        for tool_key, info in sorted(locations_map.items()):
            locations.append({
                "tool_key": tool_key,
                "tool_name": info["tool_name"],
                "tool_id": info["tool_id"],
                "scope": info["scope"],
                "path": info["path"],
                "hash": info["hash"],
                "mtime": info["mtime"],
                "mtime_iso": info["mtime_iso"],
                "version": info["version"],
                "is_newest": False,
            })

        if len(locations) < 2:
            results.append({
                "skill": skill_name,
                "status": "single",
                "locations": locations,
                "newest_tool_key": locations[0]["tool_key"] if locations else None,
            })
            continue

        hashes = {loc["hash"] for loc in locations}
        if len(hashes) == 1:
            status = "in_sync"
        else:
            status = "out_of_sync"

        newest_loc = max(locations, key=lambda x: x["mtime"] or 0)
        newest_loc["is_newest"] = True

        results.append({
            "skill": skill_name,
            "status": status,
            "locations": locations,
            "newest_tool_key": newest_loc["tool_key"],
        })
    return results


# ============================================================================
# Output Formatting
# ============================================================================

def format_scope_label(scope: str) -> str:
    """Format scope for display."""
    return "(user)" if scope == "user" else "(project)"


def format_human(results: list[dict], detected_tools: list[dict], verbose: bool) -> str:
    """Format results as a human-readable report."""
    lines = []

    tool_names = []
    for t in detected_tools:
        label = f"{t['name']} {format_scope_label(t['scope'])}"
        if label not in tool_names:
            tool_names.append(label)
    lines.append(f"Detected tools: {', '.join(tool_names)}")
    lines.append("")

    if not results:
        lines.append("No skills found across any detected tools.")
        return '\n'.join(lines)

    lines.append("Skill Sync Status")
    lines.append("-" * 60)

    count_sync = 0
    count_out = 0
    count_single = 0

    for entry in results:
        lines.append(f"\n  {entry['skill']}")

        if entry["status"] == "single":
            count_single += 1
            loc = entry["locations"][0]
            date_str = loc["mtime_iso"][:10] if loc["mtime_iso"] else "unknown"
            lines.append(f"    {loc['tool_name']:20s} {loc['path']}")
            lines.append(f"    {'':20s} (only installed here)")
            continue

        if entry["status"] == "in_sync":
            count_sync += 1
            for loc in entry["locations"]:
                lines.append(
                    f"    {loc['tool_name']:20s} {loc['path']:50s} "
                    f"\u2713 in sync"
                )
        else:
            count_out += 1
            for loc in entry["locations"]:
                date_str = loc["mtime_iso"][:10] if loc["mtime_iso"] else "unknown"
                if loc["is_newest"]:
                    marker = f"\u2713 newest ({date_str})"
                else:
                    marker = f"\u2717 stale  ({date_str})"
                lines.append(
                    f"    {loc['tool_name']:20s} {loc['path']:50s} {marker}"
                )

        if verbose and entry["status"] == "out_of_sync":
            lines.append(f"    {'':20s} hashes: " + ", ".join(
                f"{l['tool_id']}={l['hash'][:8]}" for l in entry["locations"]
            ))

    lines.append("")
    lines.append("-" * 60)
    parts = []
    if count_sync:
        parts.append(f"{count_sync} in sync")
    if count_out:
        parts.append(f"{count_out} out of sync")
    if count_single:
        parts.append(f"{count_single} single-tool only")
    lines.append(f"Summary: {', '.join(parts) if parts else 'no skills found'}")

    return '\n'.join(lines)


def format_json(results: list[dict], detected_tools: list[dict]) -> str:
    """Format results as machine-readable JSON."""
    tool_list = [
        {
            "id": t["id"],
            "name": t["name"],
            "scope": t["scope"],
            "path": str(t["path"]),
        }
        for t in detected_tools
    ]

    clean_results = []
    for entry in results:
        clean_locs = []
        for loc in entry["locations"]:
            clean_locs.append({
                "tool_key": loc["tool_key"],
                "tool_name": loc["tool_name"],
                "tool_id": loc["tool_id"],
                "scope": loc["scope"],
                "path": loc["path"],
                "hash": loc["hash"],
                "mtime_iso": loc["mtime_iso"],
                "version": loc["version"],
                "is_newest": loc["is_newest"],
            })
        clean_results.append({
            "skill": entry["skill"],
            "status": entry["status"],
            "newest_tool_key": entry["newest_tool_key"],
            "locations": clean_locs,
        })

    output = {
        "version": VERSION,
        "detected_tools": tool_list,
        "skills": clean_results,
        "summary": {
            "total": len(clean_results),
            "in_sync": sum(1 for r in clean_results if r["status"] == "in_sync"),
            "out_of_sync": sum(1 for r in clean_results if r["status"] == "out_of_sync"),
            "single": sum(1 for r in clean_results if r["status"] == "single"),
        },
    }
    return json.dumps(output, indent=2)


# ============================================================================
# Main
# ============================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Skill Sync Status Reporter — read-only diagnostic tool "
                    "that inventories skills across AI tools and reports sync status.",
    )
    parser.add_argument(
        '--project-dir',
        help='Also scan project-level skill directories under this path',
    )
    parser.add_argument(
        '--skill',
        help='Check a specific skill only (instead of all)',
    )
    parser.add_argument(
        '--json', action='store_true', dest='json_output',
        help='Output as JSON instead of a human-readable table',
    )
    parser.add_argument(
        '--verbose', action='store_true',
        help='Show per-file details for out-of-sync skills',
    )
    parser.add_argument(
        '--version', action='version', version=f'%(prog)s {VERSION}',
    )

    args = parser.parse_args()

    project_dir = Path(args.project_dir).resolve() if args.project_dir else None
    if project_dir and not project_dir.is_dir():
        print(f"Error: --project-dir path does not exist: {project_dir}", file=sys.stderr)
        sys.exit(1)

    detected = detect_tools(home=None, project_dir=project_dir)
    if not detected:
        if args.json_output:
            print(format_json([], []))
        else:
            print("No supported AI tools detected on this system.")
            print("Looked for skills directories for: " + ", ".join(
                t["name"] for t in TOOLS
            ))
        sys.exit(0)

    inventory = build_inventory(detected)

    if args.skill:
        if args.skill in inventory:
            inventory = {args.skill: inventory[args.skill]}
        else:
            if args.json_output:
                print(format_json([], detected))
            else:
                print(f"Skill '{args.skill}' not found in any detected tool.")
            sys.exit(0)

    results = compare_inventory(inventory)

    if args.json_output:
        print(format_json(results, detected))
    else:
        print(format_human(results, detected, args.verbose))


if __name__ == '__main__':
    main()
