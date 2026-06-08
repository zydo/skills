# skills

A personal collection of [Agent Skills](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview).

## Available Skills

### [agent-readable](skills/agent-readable/)

Use `agent_help(target)` to inspect a Python API's live runtime signature and author-supplied usage rules before writing code against it — so you stop guessing at call ordering or which method to pick. Also teaches how to annotate your own Python APIs with `__agent_notes__()` and ship contract tests that catch doc drift.

```
npx skills add zydo/skills --skill agent-readable
```

## License

[MIT](LICENSE)
