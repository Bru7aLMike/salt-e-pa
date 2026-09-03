---
type: schema
purpose: Contracts that shipped memory files must honor, one section per contract.
---

# Schemas

<!-- Contracts that shipped memory files must honor. One section per contract. -->
<!-- Placeholder-only template; the prose here is authored, not user data. -->

## BRIEFING block-ownership contract

<!-- BRIEFING.md has two independent producers that must never clobber each -->
<!-- other. The rule: each producer rewrites ONLY its own region, and the -->
<!-- delimiter comments are the boundary between regions. -->

<!-- Producer 1, the workspace scanner, owns the ACTIVITY block. That block is -->
<!-- delimited by the marker pair PA_SCAN:start and PA_SCAN:end. The scanner -->
<!-- rewrites everything between those two markers and nothing outside them. -->

<!-- Producer 2, the HAS handoff pipeline, owns the hand-authored region -->
<!-- (session reminders and active handoffs). HAS never writes between the -->
<!-- scanner markers, and the scanner never writes into the HAS region. -->

<!-- Boundary rule: the marker comments themselves are load-bearing. Neither -->
<!-- producer may move or delete the other producer's markers. A producer that -->
<!-- cannot locate its own marker pair must fail rather than overwrite the file. -->

| Region | Owner | Boundary |
| --- | --- | --- |
| Activity block | Workspace scanner | Between the PA_SCAN:start and PA_SCAN:end markers |
| Hand-authored region | HAS handoff pipeline | Everything outside the scanner markers |
