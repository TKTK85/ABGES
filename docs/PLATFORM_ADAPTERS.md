# Platform adapter policy

ABGES is platform-neutral. `AGENTS.md`, `docs/CONTEXT_PROTOCOL.md`, schemas, configurations, and task YAML files are authoritative.

A platform adapter may only: point to the canonical protocol, expose the platform's discovery metadata, and provide a native invocation example. It must not duplicate or override workflow rules, safety limits, model logic, context policy, or status semantics.

Current adapters:

- `CLAUDE.md` for Claude-compatible environments.
- `.agents/skills/auto-breed-geneval/` for Codex repository discovery.

Add another platform only as a thin adapter that points back to the canonical documents.