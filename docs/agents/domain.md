# Domain Docs

Jev Linux uses a single-context documentation layout.

## Before exploring, read these

- `GLOSSARY.md` at the project root, when present.
- ADRs under `docs/adr/` that concern the area being changed.

If these files do not exist, proceed silently. Do not flag their absence or create placeholders. The `/domain-modeling` skill, reached through the relevant engineering workflows, creates them when terms or decisions are resolved.

## File structure

```text
jev-linux/
├── GLOSSARY.md              # created when domain terms are resolved
├── docs/
│   ├── agents/
│   │   ├── issue-tracker.md
│   │   └── domain.md
│   └── adr/                 # created when architectural decisions are recorded
├── jev_linux/               # Python application
└── tests/                   # unittest regression tests
```

## Use the glossary's vocabulary

Use each domain concept as defined in `GLOSSARY.md` when naming issues, proposals, hypotheses, tests, and code. Avoid conflicting synonyms. If a needed concept is missing, check whether it belongs to the domain before recording the gap for domain modeling.

## Flag ADR conflicts

Surface a proposed change that contradicts an existing ADR explicitly, with the reason to revisit that decision. Do not silently override it.
