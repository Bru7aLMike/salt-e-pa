---
type: checklist
purpose: Import the source rows into the staging table and switch the report over.
workstream_id: demo-ws
status: open
created: 2026-09-21
session_id: fixture-session-0001
---

# import-run

<!-- One checklist per task, kept at tasks/<workstream_id>/<task-slug>/CHECKLIST.md in the working directory. -->
<!-- Rules: the "Task checklists" section of CLAUDE.md. status is open until the edit that ticks the last item. -->

- [x] Export the source rows to a CSV file
- [x] Verify the CSV header names match the table
- [x] Load the CSV rows into the staging table
- [ ] Reconcile the staging row count with the source
- [ ] Point the weekly report at the staging table
  - [ ] Recheck the date columns in the weekly report
- [ ] Delete the temporary CSV export
