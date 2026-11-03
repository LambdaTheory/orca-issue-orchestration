# Issue tracker: Local Markdown

**Local changes:** The historical tracker-based Wayfinder conversation type was `clarification`; new MCP Maps use `grilling`.

Issues and specs for this repo live as markdown files in `.scratch/`.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`
- The spec is `.scratch/<feature-slug>/spec.md`
- Implementation issues are one file per ticket at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`, never a single combined tickets file
- Triage state is recorded as a `Status:` line near the top of each issue file (see `triage-labels.md` for the role strings)
- Comments and conversation history append to the bottom of the file under a `## Comments` heading

## When a skill says "publish to the issue tracker"

Create a new file under `.scratch/<feature-slug>/` (creating the directory if needed).

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. The user will normally pass the path or the issue number directly.

## Legacy Wayfinding history recovery only

This section is **read-only history recovery only**, formerly used by legacy `/wayfinder`. It is **not for new MCP Maps** and must **not be used as a new-Map initializer during installation**. New Maps use the Wayfinder MCP contract; do not execute the historical create/claim/resolve/write commands below, migrate old Maps, or dual-write them. Read existing issues/files only to recover their history.

The following quoted syntax describes the old representation, not current instructions:

> Historical `/wayfinder` representation: The **map** is a file with one **child** file per ticket.
>
> - **Map**: `.scratch/<effort>/map.md` (the Notes / Decisions-so-far / Fog body).
> - **Child ticket**: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records the ticket type (`research`/`prototype`/`clarification`/`task`); a `Status:` line records `claimed`/`resolved`.
> - **Blocking**: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every file it lists is `resolved`.
> - **Frontier**: scan `.scratch/<effort>/issues/` for files that are open, unblocked, and unclaimed; first by number wins.
> - **Claim**: set `Status: claimed` and save before any work.
> - **Resolve**: append the answer under an `## Answer` heading, set `Status: resolved`, then append a context pointer (gist + link) to the map's Decisions-so-far in `map.md`.
>
