# ADR-001: Deterministic Python core; the LLM writes language only, from a cache

Status: accepted, 2026-11-13

## Context

The pipeline is scored on a hidden holdout that must be processed by the same code,
unchanged, in one command. Detection, prioritisation and the autonomy rules must be
reproducible and explainable line by line. LLM output is neither deterministic nor
guaranteed to be available during the holdout run.

## Decision

- Everything that decides is plain Python with fixed parameters: loading, detection,
  episode grouping, prioritisation, the autonomy policy, planning and verification.
- An LLM is used only to phrase text that a rule has already decided: guest messages in
  the guest's language and the prose of the Level 3 report.
- Every LLM output is stored in a committed cache keyed by a hash of its full input.
  A run reads the cache first. On a cache miss it falls back to a template text and
  lowers `confidence`; it never fails and never blocks on the network.
- Any stochastic step takes its seed from the settings object.

## Consequences

- Two runs on the same data produce byte-identical `decisions.jsonl` and reports.
- The LLM cannot change an action, an autonomy level or an approval flag.
- Guest messages for inputs not seen before the holdout use templates. That is a
  visible, accepted quality cost.
