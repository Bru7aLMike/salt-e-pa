---
type: index
purpose: Top-level index for the file-based memory tree; one row per section, pointing down to each section index.
---
<!-- OKF v0.2: `index.md` is a reserved filename. This scaffold keeps the -->
<!-- harness-recognized name MEMORY.md and marks it `type: index` rather than -->
<!-- renaming or duplicating to index.md. -->

# Memory Index

<!-- Top-level index for the file-based memory tree. One row per section. -->
<!-- Each section has its own INDEX file; go there for detail. This file -->
<!-- never duplicates section content, it only points down to it. -->
<!-- Placeholder-only template. Fill with your own; ship no real values. -->

| Section | Purpose |
| --- | --- |
| content/personal | {{SECTION_PURPOSE}} |
| content/work | {{SECTION_PURPOSE}} |
| content/entrepreneurial | {{SECTION_PURPOSE}} |
| system/workspace | {{SECTION_PURPOSE}} |
| system/rules | {{SECTION_PURPOSE}} |
| system/_internal | {{SECTION_PURPOSE}} |

## Self-architecture legend

<!-- Fixed for every install: describes the scaffold's own structure, not user data. -->
<!-- Tier-0 index and config files live at the memory root; the two tiers below hold everything else. -->

| Entry | Role |
| --- | --- |
| BRIEFING.md | Session-start dashboard: hand-authored status plus the generated activity block. |
| MAP.md | Generated orientation map: entry point for topic and alias lookup. |
| DEADLINES.md | Generated deadline register: due dates the scanner collects across the tree. |
| INTEGRITY.md | Generated health report: CRITICAL and WARN findings from the last scan. |
| SCHEMAS.md | Canonical schemas for handoffs, lifecycle-state, and the generated files. |
| MIGRATIONS.md | Append-only log of structural memory migrations. |
| system/ | The assistant's own machinery: _internal, rules, workspace. |
| content/ | The user's life: work, personal, entrepreneurial. |
