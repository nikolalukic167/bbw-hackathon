# ADR-003: The rules decide first; a supervisor LLM may add a few checked decisions

Status: accepted, 2026-11-13. Partly supersedes ADR-001. ADR-001 still holds for every
decision the rules take. It no longer holds in the strict form "an LLM only phrases text":
a supervisor LLM may now add decisions, within the limits below.

## Context

The rule lanes (S01-S07) each see one domain. What they miss is mostly *between* domains:
- two lanes acting on one asset or one person on the same day;
- a problem that keeps coming back across episodes;
- a data glitch that one lane reads as a fault.

A model that reads the whole building at one moment can name such cases. But an LLM is not
deterministic, may be unavailable during the holdout run, and can invent numbers, causes or
staff.

The mentor's guidance for using an LLM in the scored pipeline:
- the output stays (nearly) the same `decisions.jsonl`;
- the model and its version are documented;
- grounding is what gets judged: every claim must trace back to the data.

## Decision

**Rules first.** The supervisor runs after the rules, the lanes, the arbiter and the
follow-ups (`pipeline._supervise`, `src/plato40/supervise.py`). It never changes or removes
a decision. It may only add decisions that follow up decisions it was shown.

**Review points.**
- `daily`: every local day at 07:00, over the last 24 h of the building.
- `hotspot`: an asset that got decisions of two or more issue types within 24 h, reviewed
  at the later decision's time, once per asset and local day.
  - Energy pre-heat (`energy_optimization`) never makes a hotspot, because it touches most
    rooms every day.

**Input.** At review time `t` the model sees only what was known at `t` (ADR-002):
- the decisions of the last 24 h (at most 30);
- the context snapshot (`src/plato40/context.py`): building state, the assets reviewed,
  metrics that moved, and the evidence `refs` behind every number.

**Output.** One tool answer `{assessment, add}`. `add` holds at most 2 decisions, and only
the low-risk actions `flag_anomaly`, `request_human_approval`, `request_inspection` or
`defer_action`. Each added decision:
- names in `about` the event IDs it follows up; these become its `dependencies`, and it
  inherits its issue type and episode from the first one;
- carries a `reason` that starts with "Supervisor review (<model>):", so every added row
  shows which model wrote it;
- gets `confidence` 0.6.

**Checks.** An answer is used only if every added decision passes all of these. Otherwise
nothing from that review is added:
1. **Schema:** exactly `{assessment, add}`, with the item fields of the tool schema.
2. **Enums:** action from the four above; priority from the contract.
3. **Assets:** IDs from `building.json` that were shown in the input.
4. **`about`:** non-empty, only event IDs that were shown.
5. **Evidence:** non-empty, every ref copied exactly from the `refs` it was shown.
   This is the grounding rule: the model cannot cite a row the snapshot did not give it.
6. **Numbers and IDs:** every number, time and ID in `reason` and `check` appears in its
   input (the `llm_guard` patterns).
7. **Policy and contract:** the policy sets autonomy and the approval flags exactly as for
   the rules' decisions (`policy.apply`), and the decision passes the contract check. A
   proposal never sets them itself.

**Cache and fallback.**
- An answer is cached under sha256(model + prompt version + system prompt + tool schema +
  input), in
  `<llm-cache>/supervisor/`.
- Only answers that pass are cached. A rejected answer goes to `supervisor/rejected/`
  for the trace and is never reused.
- `PLATO40_LLM=off` (the default) reads the cache only. A cache miss adds nothing.
- A network error, quota error or timeout adds nothing; the rules' decisions stand.
- A model that answers in prose instead of the tool is asked once more. If it does so
  twice, the review adds nothing.
- Without `--llm-cache`, or with `--no-supervisor`, the supervisor does not run.

**Models** (named in the README; overridable by env var):
- **Supervisor:** `claude-sonnet-5-5` (`PLATO40_SUPERVISOR_MODEL`), prompt `supervisor/2`.
  It uses `tool_choice: auto` with one retry, because Sonnet 5.5 refuses a forced tool
  choice.
- **Guest messages:** `claude-haiku-5-5` (`PLATO40_LLM_MODEL`), prompt `guest_message/4`
  (ADR-001, `llm_guard.py`), with the tool forced.
- **Temperature:** these models take no temperature parameter. Determinism comes from the
  cache, not from sampling settings.

**Log.** `plato40 run` writes `<out>.supervisor.jsonl`, one row per review point:
- the review kind, `t`, the asset and the source (`cache`, `llm` or `none`);
- the model's `assessment`;
- the added event IDs, or why nothing was added (`no cached answer; LLM off`, a failed
  check, or a call failure).

This is the audit trail for the jury: it shows reviews that added nothing as well as those
that did.

## Consequences

- **Nearly the same decisions.** The supervisor only appends rows, at most 2 per review,
  and only in the four low-risk actions. On the quickstart live run (Sonnet 5.5):
  - 24 reviews added 2 decisions;
  - 1 answer was rejected for an invented number;
  - 2 answers came back in prose twice and added nothing.
- **Reproducible.**
  - For the holdout, the pipeline runs once live (`PLATO40_LLM=on`, with the key) and the
    cache is committed.
  - The rerun with the LLM off is byte-identical to the live run (checked on quickstart).
  - Without the key and without a cache, the output is the rules' decisions alone. It is
    still valid and complete.
- **Cache keys depend on the input.** Any change to the decision lists or to the snapshot
  format changes the input, so the final regeneration needs one more live run.
  - That is also why the snapshot keeps the key `cop_ratio`, explained in the prompt's
    glossary ("HP1's COP divided by its own reference COP; 1.0 is normal"), instead of
    renaming it.
- **Grounding is checkable.** Every added row cites only refs that the snapshot built from
  rows known at `t`. Every number in its reason appears in its input. Its dependencies
  point at real decisions.
- **What ADR-001 still guarantees.**
  - The LLM cannot change an action, an autonomy level or an approval flag of any
    decision.
  - Detection, prioritisation and the policy stay plain Python.
- **Cost.** One call per review point: roughly one daily review a day plus a few hotspots,
  run 16 at a time.
