# Issue tracker: Local Markdown

Issues and specs for Jev Linux live as Markdown files in `.scratch/`, relative to the project root.

The `.scratch/` directory is ignored by Git because issues may include private
notes and conversation history. Keep it local; move any deliberately public
documentation into `docs/` after reviewing its contents.

## Conventions

- One feature per directory: `.scratch/<feature-slug>/`.
- The spec is `.scratch/<feature-slug>/spec.md`.
- Implementation issues are one file per ticket at `.scratch/<feature-slug>/issues/<NN>-<slug>.md`, numbered from `01`; do not combine tickets into one file.
- Record a `Status:` line near the top of each issue file to describe its current state. Triage role labels are not configured in this setup.
- Append comments and conversation history under a `## Comments` heading.

## When a skill says "publish to the issue tracker"

Create a new file under `.scratch/<feature-slug>/`, creating the directory if needed.

## When a skill says "fetch the relevant ticket"

Read the file at the referenced path. Resolve a bare issue number within its feature directory; ask for the feature if the number is ambiguous.

## Wayfinding operations

Used by `/wayfinder`. The map is a file with one child file per ticket.

- Map: `.scratch/<effort>/map.md`, with Notes, Decisions-so-far, and Fog sections.
- Child ticket: `.scratch/<effort>/issues/NN-<slug>.md`, numbered from `01`, with the question in the body. A `Type:` line records `research`, `prototype`, `grilling`, or `task`; a `Status:` line records `open`, `claimed`, or `resolved`.
- Blocking: a `Blocked by: NN, NN` line near the top. A ticket is unblocked when every listed ticket is `resolved`.
- Frontier: scan the effort's issue directory for open, unblocked, unclaimed tickets; choose the first by number.
- Claim: set `Status: claimed` and save before working on the ticket.
- Resolve: append the answer under an `## Answer` heading, set `Status: resolved`, then append a context pointer (gist and link) to Decisions-so-far in the map.
