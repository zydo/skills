---
name: agent-readable
description: Use this skill for Python and TypeScript when a user wants to understand an unfamiliar API before coding against it — especially internal libraries, third-party SDKs, recently-upgraded dependencies, or APIs with confusing similar-sounding methods or required call ordering — by getting live runtime introspection rather than relying on docs or guessing. Also use when designing a Python or TypeScript class and wanting to annotate it with lifecycle rules, preconditions, or method-ordering constraints that agents and developers can discover via introspection. Always trigger for any mention of agent_help, agentHelp, __agent_notes__, agentNotes, AgentReadableMixin, or agent-readable. Skip for familiar stdlib, CSS/UI, pure algorithms, and non-Python/TypeScript code.
---


# agent-readable (Python + TypeScript)

Two sibling libraries, one philosophy: before coding against an unfamiliar API,
**inspect it at runtime** to get the live public surface and any author-supplied
behavioral rules — instead of guessing from stale training data.

| | Python | TypeScript / JS |
|---|---|---|
| Library | `agent-readable` | `agent-readable-ts` |
| Install | `pip install agent-readable` | `npm install agent-readable-ts` |
| Inspect function | `agent_help(target)` | `agentHelp(target)` |
| Author notes method | `__agent_notes__()` classmethod | `agentNotes()` method |
| Custom output method | `__agent_help__()` | `agentHelper()` |
| Protocol / mixin | `AgentReadable`, `AgentReadableMixin` | `AgentHelper`, `AgentNoter` interfaces |

***

## When to activate

Call the inspect function before writing code against an API where guessing could
plausibly be wrong — concretely, when:

* The target is **project-local or third-party code you don't already know cold**.
* It's **stateful, version-sensitive, poorly documented, has lifecycle or ordering
  constraints, or exposes several similar entrypoints** that are easy to confuse.
* You're **adding or changing a public API** meant for reuse by humans or agents.
* The user mentions `agent_help`, `agentHelp`, `__agent_notes__`, `agentNotes`,
  or the `agent-readable` / `agent-readable-ts` library.

## When not to activate

Skip it for trivial builtins (`len`, `dict`, `console.log`), stdlib usage you
know cold, routine calls into familiar libraries (a basic `requests.get`, a
`fs.readFile`), and pure algorithmic code with no external API. Reserve it for
APIs where getting usage wrong is plausible.

**Language gate:** this skill is for Python and TypeScript/JavaScript only.
Do not apply it when working in other languages.

***

## Safety: imports and side effects

`agent_help()` / `agentHelp()` must import or require the target module — and
import-time code runs. It may open connections, touch files, read env vars, or
do heavy init. So:

* Run only in a **trusted project environment**.
* Don't blindly import untrusted, expensive, or destructive modules just to get
  help.
* If import is unsafe, read source/tests/type hints instead.

The output reflects the object **as installed here, now**. If code will run
elsewhere (prod, CI), verify the package version there — the inspected API may
differ.

***

## Python — Job 1: Consume

```python
from agent_readable import agent_help

print(agent_help(SomeClass))      # class — constructor, public API, notes
print(agent_help(some_instance))  # instance — dispatches to its class
print(agent_help(some_module))    # module — docstring + public functions/classes
print(agent_help(some_func))      # function or method — signature + docstring
```

From a shell:

```bash
python -c "from agent_readable import agent_help; import target_lib; print(agent_help(target_lib.SomeClass))"
# or via the CLI:
python -m agent_readable sqlite3:Connection
python -m agent_readable pathlib
python -m agent_readable json:dumps
```

### Reading the output

| Section | Source | How to treat it |
|---|---|---|
| `## Signature` / `## Constructor` | live introspection | **Ground truth** for the current call shape |
| `## Public API` | live introspection | Authoritative for which names exist |
| `## Purpose`, per-method summaries | docstrings | As accurate as the author's docstrings |
| `## Agent usage rules` | library boilerplate | Generic guardrails; don't invent behavior |
| `## Notes from class <X>` | `__agent_notes__()` | Behavioral guidance, if supplied and maintained |

If a note contradicts a visible signature, trust the signature. Among multiple
`## Notes from class` sections, the **leaf class wins**.

***

## Python — Job 2: Author

Preference order: **good docstrings → focused `__agent_notes__()` → (rarely) `__agent_help__()`**

### Docstrings first

`agent_help()` reads docstrings directly: the class docstring becomes `## Purpose`,
each method's first paragraph its `## Public API` summary.

### `__agent_notes__()` — cross-method rules only

Add a `classmethod` for rules that span methods:

```python
class Sensor:
    """Reads a value from a hardware sensor."""

    def __init__(self, pin: int, *, unit: str = "C"): ...
    def calibrate(self, offset: float): ...
    def read(self) -> float: ...

    @classmethod
    def __agent_notes__(cls) -> str:
        return """
## Do
- Call `calibrate()` once during setup, before the first `read()`.

## Do not
- Do not call `read()` before `calibrate()` on first use.
"""
```

**Belongs here:** lifecycle/ordering, preconditions, cleanup, sync vs async,
streaming vs non-streaming, do/don't lists.

**Doesn't belong here:** per-method behavior (→ that method's docstring),
duplicated docstring content.

Only add `__agent_notes__` when signatures + docstrings leave room for misuse.
If the API is straightforward, skip them — they add maintenance burden.

Notes accumulate across the MRO (leaf wins on conflict). Don't call `super()` —
collection is automatic. `AgentReadableMixin` only adds IDE hints; it's not required.

### Make notes verifiable

Anchor notes to real members and ship a contract test:

```python
import re
from your_module import Sensor

def test_agent_notes_reference_real_members():
    for name in re.findall(r"`(\w+)\(\)`", Sensor.__agent_notes__()):
        assert hasattr(Sensor, name), f"note references missing member: {name}"
```

A rule worth writing down is usually worth asserting the behavior itself too
(e.g. that `read()` raises before `calibrate()`).

### Custom `__agent_help__()` — rarely

Replaces the entire auto-generated output. Use only when you have a hand-formatted
string to ship verbatim.

**Footgun:** Never define both `__agent_help__()` and `__agent_notes__()` on one
class — the custom help owns the output and notes are dropped. The library warns,
but warnings get swallowed in agent shells. Treat "both defined" as a hard error.

***

## TypeScript — Job 1: Consume

```typescript
import { agentHelp } from 'agent-readable-ts';

console.log(agentHelp(SomeClass));        // class constructor
console.log(agentHelp(someInstance));     // instance — dispatches to its class
console.log(agentHelp(someFn));           // function or arrow function
console.log(agentHelp(someObject));       // plain object
```

### Install

```bash
npm install agent-readable-ts
```

Node 20+, no runtime dependencies.

### CLI usage

```bash
# List all exports of a package
npx agent-readable-ts commander

# Document a specific export
npx agent-readable-ts commander:Command

# Local TypeScript file
npx agent-readable-ts ./src/widget.ts:Widget

# Package not installed in the current project: opt in to on-demand fetch
npx agent-readable-ts --install left-pad
```

The CLI mitigates TypeScript's runtime reflection limits by parsing `.ts` source
directly or reading adjacent `.d.ts` declaration files for `.js` packages.

Packages already installed in the project load directly. For anything else the
CLI refuses to fetch unless you pass `--install`, which runs `npm install` with
`--ignore-scripts` into an isolated cache (`~/.cache/agent-readable-ts`, override
with `AGENT_READABLE_CACHE`) — never into the project's `node_modules`. Cached
packages load offline without needing `--install` again.

### TypeScript runtime limitations

TypeScript has fundamental reflection constraints that Python doesn't:

* Type annotations and return types are **unrecoverable from compiled JS** —
  the CLI's source-parsing fills this gap.
* Parameter names fall back to `arg0`, `arg1` when unavailable.
* Private TypeScript members detected via underscore prefix only (imperfect).
* JavaScript `#private` fields are completely unreflectable.
* Constructors and getters are not invoked during introspection.

When these constraints matter, prefer the CLI (`npx agent-readable-ts`) over the
programmatic `agentHelp()` — the CLI can read the original `.ts` source.

***

## TypeScript — Job 2: Author

Preference order: **good JSDoc → focused `agentNotes()` → (rarely) `agentHelper()`**

### JSDoc first

Document your public API with JSDoc. `agentHelp()` surfaces these alongside the
method signatures.

### `agentNotes()` — cross-method rules

Add an `agentNotes()` method for rules that span methods:

```typescript
import { AgentNoter } from 'agent-readable-ts';

class DatabasePool implements AgentNoter {
    constructor(config: PoolConfig) { /* ... */ }
    acquire(): Promise<Connection> { /* ... */ }
    release(conn: Connection): void { /* ... */ }
    shutdown(): Promise<void> { /* ... */ }

    agentNotes(): string {
        return `
## Do
- Always call \`release(conn)\` after every \`acquire()\`, even on error.
- Call \`shutdown()\` during graceful application teardown.

## Do not
- Do not call \`acquire()\` after \`shutdown()\` — it throws.
- Do not share a \`Connection\` object across async tasks.
`;
    }
}
```

Notes **accumulate across the inheritance chain** in parent-to-child order (TypeScript,
unlike Python, doesn't automatically collect from the prototype chain — implement
`agentNotes()` on each class that has cross-method rules).

**Belongs here:** lifecycle/ordering, preconditions, cleanup, async/Promise
constraints, do/don't lists.

### Custom `agentHelper()` — rarely

Implements the `AgentHelper` interface and **replaces** the entire auto-generated
output. Use only when you have a fully hand-crafted usage guide to ship verbatim.

**Footgun:** If both `agentHelper()` and `agentNotes()` are defined on the same
class, `agentHelper()` wins and `agentNotes()` is dropped. The library emits a
warning via `setWarnOutput()`, but warnings can be silenced or swallowed in agent
shells. Treat "both defined" as a hard error.

***

## The one rule

> Before coding against an unfamiliar or risky **Python or TypeScript** API, call
> `agent_help(target)` (Python) or `agentHelp(target)` / the CLI (TypeScript):
> trust the **signatures**, sanity-check the **notes**, mind **imports and
> version**. When you author an API, make the notes verifiable so they fail
> loudly instead of lying quietly.
