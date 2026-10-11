# skills

A personal collection of [Agent Skills](https://platform.claude.com/docs/en/agents-and-tools/agent-skills/overview).

## Available Skills

### [agent-readable](skills/agent-readable/)

For Python and TypeScript. Generates accurate, token-efficient help messages from live runtime introspection so coding agents stop guessing at API signatures and usage rules. Also teaches how to annotate your own APIs with agent-oriented behavioral notes.

```
npx skills add zydo/skills --skill agent-readable
```

### [session-nudge](skills/session-nudge/)

For Claude Code, Codex CLI and Grok Build on macOS, Linux and Windows. A background OS job types a periodic "continue working" nudge into the terminals of opted-in agent sessions, so long-running sessions keep going instead of idling. Turn it on or off per session, set each session’s pace in plain language ("nudge every 20 minutes"), schedule one reminder at a local time, and let the first run install the scheduler job (launchd, systemd timer or cron, Task Scheduler) and verify delivery end to end.

```
npx skills add zydo/skills --skill session-nudge
```

## License

[MIT](LICENSE)
