---
type: schema
purpose: Fixture schema file for the checklist pointer test; holds the handoff frontmatter section HAS reads.
---

# Schemas (fixture)

## 2. Handoff frontmatter

| Field | Type | Rule |
|---|---|---|
| `date` | YYYY-MM-DD | Session date. |
| `session_end` | ISO 8601 with offset | Session end time. |
| `workstream_id` | string | Must match a workstream README `workstream_id`. |
| `status` | `active`, `dormant`, `blocked` or `complete` | Resolved workstream status. |
| `next` | list of strings | Next actions. Ticketed items (leading `[A-Z]+-\d+` key) carry no carry tags and no `[unverified]` tag; the ticket tracker owns their lifecycle. |
| `blockers` | list of strings, or the literal `none` | Current blockers. |
| `open_items` | list of strings, or the literal `none` | Open questions and pending decisions. |

## 3. End of fixture
