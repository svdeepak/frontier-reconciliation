# Reconciliation Exception Triage Agent

An agentic workflow that reconciles an internal ledger (Source A) against a
partner statement (Source B), detects row-level breaks, classifies their
cause, and produces an evidence-backed exception report (XLSX) for human
review. Built for the micro1 Frontier Engineering Challenge 2026.

**User:** finance/operations analysts reconciling transactions between two sources.
**Core hypothesis:** aggregate correctness != row-level correctness.
**Evaluation contract:** see docs/EVALUATION.md (frozen before implementation).

Sections to be completed in later milestones: architecture, results,
improvement changelog, hot take, reproduction guide, model/tool disclosure.
