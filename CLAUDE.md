# Project Instructions for AI Agents

This file provides instructions and context for AI coding agents working on this project.

<!-- BEGIN BEADS INTEGRATION v:1 profile:minimal hash:46cd31e7 -->
## Beads Issue Tracker

This project uses **bd (beads)** for issue tracking. Run `bd prime` to see full workflow context and commands.

### Quick Reference

```bash
bd ready              # Find available work
bd show <id>          # View issue details
bd update <id> --claim  # Claim work
bd close <id>         # Complete work
```

### Rules

- Use `bd` for ALL task tracking — do NOT use TodoWrite, TaskCreate, or markdown TODO lists
- Run `bd prime` for detailed command reference and session close protocol
- Use `bd remember` for persistent knowledge — do NOT use MEMORY.md files

**Architecture in one line:** issues live in a local Dolt DB; sync uses `refs/dolt/data` on your git remote; `.beads/issues.jsonl` is a passive export. See https://github.com/gastownhall/beads/blob/main/docs/core-concepts/sync-concepts.md for details and anti-patterns.

## Agent Context Profiles

The managed Beads block is task-tracking guidance, not permission to override repository, user, or orchestrator instructions.

- **Conservative (default)**: Use `bd` for task tracking. Do not run git commits, git pushes, or Dolt remote sync unless explicitly asked. At handoff, report changed files, validation, and suggested next commands.
- **Minimal**: Keep tool instruction files as pointers to `bd prime`; use the same conservative git policy unless active instructions say otherwise.
- **Team-maintainer**: Only when the repository explicitly opts in, agents may close beads, run quality gates, commit, and push as part of session close. A current "do not commit" or "do not push" instruction still wins.

## Session Completion

This protocol applies when ending a Beads implementation workflow. It is subordinate to explicit user, repository, and orchestrator instructions.

1. **File issues for remaining work** - Create beads for anything that needs follow-up
2. **Run quality gates** (if code changed) - Tests, linters, builds
3. **Update issue status** - Close finished work, update in-progress items
4. **Handle git/sync by active profile**:
   ```bash
   # Conservative/minimal/default: report status and proposed commands; wait for approval.
   git status

   # Team-maintainer opt-in only, unless current instructions forbid it:
   git pull --rebase
   bd dolt push
   git push
   git status
   ```
5. **Hand off** - Summarize changes, validation, issue status, and any blocked sync/commit/push step

**Critical rules:**
- Explicit user or orchestrator instructions override this Beads block.
- Do not commit or push without clear authority from the active profile or the current user request.
- If a required sync or push is blocked, stop and report the exact command and error.
<!-- END BEADS INTEGRATION -->


## Build & Test

```bash
make venv        # repo-local .venv, stamped -- re-running is free
make lint        # ruff over src tests scripts
make docs-check  # relative links, mermaid edges, and the co-located .md rule
make test        # pytest over tests/ (the JS suite skips without node + jsdom)
make build       # wheel + sdist
```

`make lint docs-check test build`, in that order, is the CI gate. `make ui-deps` installs
jsdom and turns the skipped JavaScript suite into a running one. `make check` validates
`servers.yaml` and `gateway.env` without binding a port or spawning a backend, and
`make run-dev` starts the daemon against the schema zoo, which needs no credentials.

Behind a TLS-intercepting proxy, pass `PIP_TRUSTED_HOST="pypi.org files.pythonhosted.org"`.

## Architecture Overview

A long-lived daemon serving **MCP over WebSocket and Streamable HTTP** to any number of
clients, fanning out to N backend MCP servers that are stdio subprocesses it owns. Every
credential lives in one gitignored `gateway.env`; each backend's environment is built from
nothing but an allowlist plus its own `env` and `env_passthrough`. `servers.yaml` is
committed and holds only `${NAME}` references. The same port serves the admin UI, which is
static ES modules with no build step, and `desktop/` wraps the lot in a Tauri window.

[ARCHITECTURE.md](ARCHITECTURE.md) is the module map, [GET_STARTED.md](GET_STARTED.md) is
the user-facing walkthrough, and every module under `src/mcp_gateway/` has a sibling `.md`
carrying its reasoning.

## Conventions & Patterns

- **Every module has a sibling `.md`**, and `make docs-check` fails if one goes missing or a
  relative link stops resolving. The reasoning goes there — *why this shape, and what the
  alternative cost* — not in a header comment restating the code.
- **Nothing writes a credential.** No admin method returns a value, and `gateway.env` is
  read-only to the whole program. The UI edits `servers.yaml` and only through
  `config_writer.py`.
- **`${VAR}` never resolves from `os.environ`**, and is refused outright in `command` and
  `args` — argv is world-readable through `ps`.
- **The admin UI's columns speak MCP, not `admin.*`.** A test bench is only worth having if
  what you exercise in it is byte-identical to what the model gets.
- **A user-visible change is a CHANGELOG entry** under `## Unreleased`, written as what the
  change is *for* rather than as a list of edits.
