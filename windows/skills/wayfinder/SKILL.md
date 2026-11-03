---
name: wayfinder
description: Default requirements clarification for orca-issue-orchestration. On every workflow start or resume, create or restore an MCP Map, clarify the destination and pending decisions, and resolve its decision Tickets together with the human until the way is clear.
---

<!-- Adapted from Matt Pocock's wayfinder skill (MIT) to plan through the Wayfinder MCP service. Product-side integration goes through the service's MCP contract only; the service is a separate product whose license is governed by its upstream repository, not by this file. The service owns Map state and its tool descriptions govern every state change; this file adds the method and the Orca-suite handoff, and deliberately does not restate the service's rule text. -->

**Local changes:** orca-issue-orchestration is the single entry; it invokes this skill by default and must create or restore an MCP Map on every start or resume, regardless of task size, one-turn scope or prior confirmation. Reuse the current workflow Map; do not create a new Map per message or implementation sub-Issue. Clarification Tickets surface as batched Answer Desk questions during discussion (the old one-HITL-ticket-per-session limit no longer applies inside a Map), and the ticket type used for clarification is grilling. prd-writing keeps the single PRD/SPEC; align-requirements presents its current version and collects answers. See the PRD handoff section below.

## The Map lives in the Wayfinder MCP service

The Map records every requirement entering the Orca orchestration workflow, including requirements already confirmed. Its single authoritative store is the **Wayfinder MCP service**, reached only with these tools: `map_create`, `map_list`, `map_read`, `map_apply`, `map_wait`. Nothing else is the Map:

- No credential bypass, no direct HTTP writes, no scripts that emulate the service, no GitHub or local-file substitute for Map state.
- If the tools are missing, or the service fails, **stop and say so plainly** (what was attempted, what failed, what decision or service is missing), then wait. Do not route around the failure and do not improvise a second store.
- Older maps kept as GitHub issues from before this change stay **read-only history**: never migrate them into the service, never dual-write them, and never close or edit them to fake completion. Answer recovery and history for an old map still goes through that issue tracker; only new requirements are charted in the MCP service.
- Every start or resume of orca-issue-orchestration must call this sibling skill and create or restore an MCP Map. Zero Tickets do not waive the Map: when no genuine pending decision exists, retain a zero-Ticket Map rather than inventing a Ticket or skipping the Map. Reuse prior explicit decisions and authorizations as existing evidence within genuine Tickets; do not ask them again or invent Tickets merely to record them.

## Plan, don't do

A Map is **planning**. Each Ticket resolves a decision; planning hands off to implementation outside the Map. Keep a Claim only while waiting for the human's reply to that Ticket, and apply writes only against the latest observed Revision; on a conflict, reread, report the new Revision and stop — never replay, merge, or take over unasked.

**Completion has no close button.** The service exposes no Map-close tool: do not invent one, and treat an empty Frontier as **not** evidence of completion. The requirement phase is done only when the Map shows every relevant Ticket settled, no blocking Fog, and there is no undecided in-scope choice blocking handing the plan to its implementer — checked against a current `map_read`, not against memory.

## Talking to the human

Refer to Maps and Tickets by **title** in everything the human reads; keep stable IDs for tool calls and may show an ID beside a title, never instead of it. Tell them outcomes and what you need from them; Claims, Revisions and Settlements are bookkeeping and stay out of replies. When you need a verdict, ask the question and do not explain the rule that makes you wait.

## The Map

The Map is an **index, not a store**; a decision lives in exactly one place, its Ticket's Settlement. Each part holds one job:

| Part | Holds |
| --- | --- |
| Destination | What reaching the end looks like: the spec, decision or change this effort finds its way to. |
| Notes | Domain, skills every session should consult, standing preferences and constraints — never decisions. |
| Tickets | One open question each; settled Tickets are the record of decisions. |
| Fog | In-scope questions you can see coming but cannot state precisely yet. |
| Scope Exclusions | Work ruled beyond the Destination; it never graduates. |

Tickets carry a type `research` (AFK, settled with a sourced Finding), `grilling` (HITL conversation, the default — clarification questions land here), `prototype` (HITL, a cheap artifact to react to) or `task` (manual work that unblocks a decision, never builds the Destination). A HITL Ticket resolves only through the human's verdict on the specific answer proposed; the agent never supplies the human's side. Blocking uses Ticket Dependencies; the **Frontier** is what `map_read` returns as open, unblocked, unclaimed Tickets.

**Publish in batches, not one at a time.** Post every currently askable human question to the Answer Desk **at once** inside the active Map Ticket batch: each with two or three options, a full recommended answer, why it is being asked, and its triage. Claim each Ticket before posting its question. While answers are pending, keep preparing other Frontier work; never hold back an askable question to preserve a one-question-per-session rhythm.

**Orca preparation override:** follow `orca-issue-orchestration/references/preparation.md` for authorization and where requirement records live. During the planning phase, do not start development watchers, Runs or workers — the map and its Tickets are planning artifacts, not implementation tickets. When planning completes, hand the requirement back to the original orca-issue-orchestration coordinator, which proceeds with the already-authorized GitHub ticket-creation and Orca dispatch flow without asking the user to restart anything. Reuse confirmed decisions when publishing or resuming; do not re-interview them.

## PRD handoff in the Orca suite

Wayfinder owns the questions and their order; call the sibling `prd-writing/SKILL.md` for its material check and, once the key facts and decisions are ready, to organize the single requirements document. Follow `orca-issue-orchestration/references/requirements.md`. A key gap goes back to Wayfinder; do not turn an unresolved proposal into a complete PRD or run a second interview in the writer.

The PRD (or the project's existing SPEC in that role) is the single requirements source. The Map's Tickets and Settlements keep reasons and link to its requirement/acceptance IDs; `align-requirements` presents a specific PRD version and collects feedback, not a separate plan. Check response IDs and versions, then update the same PRD with valid decisions and retain the displayed snapshot. A verdict on one Ticket settles that question and does **not** approve the entire PRD; approval of the whole requirement is its own check. Reuse an explicit approval, including a single reply that selects changes and confirms their result; do not ask a second time merely because the PRD was written or versioned, and do not add an extra fixed approval ticket to a user who already made this decision. No size-based Map exemption applies in the Orca workflow. A zero-Ticket Map with no Fog may pass only with PRD synchronization, confirmed scope and implementation authorization; no artificial Ticket or extra approval is needed. The template has no built-in comments or answer export. A Map accepts human answers through the Answer Desk or an explicit conversation verdict on a specific displayed answer; each question still has a single active intake. For a Pending or Answered Desk Question answered explicitly in conversation, withdraw that exact question version with reason `answered in conversation` and settle without `deskAnswer` in the same ordered `map_apply`. Never keep both paths active or request a duplicate Desk reply. For the Desk path, bind the current Taken answer ID and question version; a duplicate reply is not a new verdict. For ordinary PRD presentation feedback, use the current conversation or an already-authorized host comment tool without opening another decision intake. A generic continue, scope comment or blanket assent to unseen recommendations is not a verdict.

## Sibling skills during clarification

When calling `prototype` during this clarification phase, the prototype and the resolved design ticket stay exploratory artifacts: do not follow its standalone fold-back/production-absorption step into real application code, and a resolved design ticket is not authorization to implement — production integration belongs to the confirmed development task. Keep `domain-modeling`'s reasoning method, but reuse the project's agreed document locations; before creating CONTEXT.md or an ADR layout where none is agreed, ask the user to confirm the location, and until then retain modeling decisions in the agreed clarification record. Reuse confirmed requirements and existing Maps instead of interviewing again; ask only the genuine, independent pending questions of the current Ticket, and never pad the batch with invented questions.

## Invocation

### Chart the Map

Any requirement entering orca-issue-orchestration, no Map yet; prior confirmation is reusable evidence, not a Map exemption.

1. **Name the Destination with the human** using grilling until it is observable and the human confirms it. It fixes the scope, so it comes first; do not create a Map on a guessed Destination.
2. **Map the frontier breadth-first.** Reuse known facts and agreed decisions; fan out across the space in one batch to surface the open decisions and the first steps takeable now.
3. **Discover, then create.** Page `map_list` to the end; if the intended Map exists, retain its stable ID and resume instead of creating a replacement. Otherwise call `map_create` once with the confirmed Destination.
4. **Publish genuine open work in one ordered `map_apply`** against the returned Revision; if there are no genuine pending Tickets or Fog, keep the valid zero-Ticket Map and continue to the handoff checks without a synthetic Ticket or empty write. Otherwise publish: the Tickets you can state precisely now, then their Dependencies, then Fog and Scope Exclusions. A Ticket question states only the question.
5. **Fire the research tickets** to sub-agents that do the reading and return summaries; sub-agents never claim or write.
6. Continue directly into the first Frontier batch in the same session when it is ready; charting does not require a session stop and does not itself settle human decisions.

### Work through a Map

1. Read the Map with `map_read`; on resumption default-read to observe the authoritative current Revision.
2. Claim a Frontier Ticket from the current read before working on it.
3. **Resolve it**, batching: publish every currently askable question at once via the Answer Desk, and keep independent work going while the human decides. If the human named a Ticket, start there; otherwise take what the Frontier offers, without a one-per-session cap.
4. Settle released verdicts and publish newly unlocked questions in the same ordered `map_apply` where its rules allow; settle a grilling Ticket with `deskAnswer` identifying the current Taken answer ID and question version, or use the explicit conversation verdict path above (withdraw Pending/Answered then settle without `deskAnswer` in the same batch). Add Tickets only for decisions an answer made precise and the route still needs; graduate the Fog it cleared; name any settled decision the answer invalidates and reopen it only on the human's verdict.
5. On resumption and at each Settlement, run the completion check: every relevant Ticket settled, no blocking Fog, no undecided in-scope choice — otherwise name the specific unfinished Ticket and keep working. When it passes, record the accepted decisions, their Map links and the PRD version, and hand the requirement back to the orca-issue-orchestration coordinator for the authorized build flow.

Other sessions may advance the same Map concurrently; the service's conflict rules decide what happens, not Orca's general retry habits.
