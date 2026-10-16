---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---

Implement the work described by the user in the spec or tickets.

Use /tdd where possible, at pre-agreed seams (or its explicit Orca orchestration exception).

Run typechecking regularly, single test files regularly, and the full test suite once at the end.

Inspect all staged, unstaged, and untracked changes before committing. Commit the implementation and tests to the current task branch locally; do not push yet. A local commit is a checkpoint, not acceptance.

For an Orca-orchestrated task, self-review the committed changes along the Standards / Spec axes without spawning sub-agents or invoking the full /code-review skill. Use the supplied fixed base and record the reviewed HEAD SHA. Fix required findings, run the relevant tests, commit, and recheck before pushing and opening the PR as directed by the task. The coordinator arranges independent /code-review after worker_done, supplying the fixed base, reviewed SHA, Issue / SPEC, and standards.

Outside Orca orchestration, use /code-review after the local commit so its HEAD-based diff includes the work. Fix required findings, test, commit, and review again before any authorized push.
