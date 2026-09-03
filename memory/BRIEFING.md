---
type: briefing
purpose: Session-start briefing with a hand-authored region and a scanner-generated activity block.
---

# Briefing

<!-- Two independent producers write this file. Each owns exactly one region -->
<!-- below and rewrites ONLY that region. The block-ownership contract is -->
<!-- documented in SCHEMAS.md. Placeholder-only template until first run. -->

## Hand-authored region

<!-- The HAS handoff pipeline owns everything in this region. The scanner -->
<!-- never writes here. -->

### Session Reminders

<!-- Standing reminders survive between sessions here. Placeholder-only. -->

- {{REMINDER}}

### Active Handoffs

<!-- One row per active handoff. -->

| Workstream | Handoff | Next |
| --- | --- | --- |
| {{WORKSTREAM}} | {{HANDOFF_FILE}} | {{NEXT_ACTION}} |

## Scanner-owned region

<!-- The workspace scanner owns everything between the two markers below and -->
<!-- writes nothing outside them. The markers are the boundary; do not move -->
<!-- or delete them. The stub ships the marker pair empty. -->

<!-- PA_SCAN:start -->

<!-- PA_SCAN:end -->
