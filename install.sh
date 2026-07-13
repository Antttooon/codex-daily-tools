#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="${CODEX_SKILLS_DIR:-$HOME/.agents/skills}"
TOOLS_DIR="${CODEX_TOOLS_DIR:-$HOME/codex-tools}"

mkdir -p "$SKILLS_DIR/daily-log" "$TOOLS_DIR"

cp "$ROOT_DIR/skills/daily-log/SKILL.md" "$SKILLS_DIR/daily-log/SKILL.md"
cp "$ROOT_DIR/scripts/export_codex_daily_to_obsidian.py" "$TOOLS_DIR/export_codex_daily_to_obsidian.py"
cp "$ROOT_DIR/scripts/backfill_codex_daily_duration.py" "$TOOLS_DIR/backfill_codex_daily_duration.py"
chmod +x "$TOOLS_DIR/export_codex_daily_to_obsidian.py"
chmod +x "$TOOLS_DIR/backfill_codex_daily_duration.py"

echo "Installed daily-log skill: $SKILLS_DIR/daily-log/SKILL.md"
echo "Installed Obsidian exporter: $TOOLS_DIR/export_codex_daily_to_obsidian.py"
echo "Installed duration backfill: $TOOLS_DIR/backfill_codex_daily_duration.py"
