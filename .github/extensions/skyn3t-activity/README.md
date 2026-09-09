# SkyN3t native activity feed

This Copilot CLI extension only observes a producer-owned JSONL file and sends
safe activity lines to the native timeline using `session.log`. It does not
launch, control, cancel, or infer the outcome of a SkyN3t process. It does not send
prompts, request sensitive environment variables, or change permissions.

## Tools

| Tool | Arguments | Result |
| --- | --- | --- |
| `skyn3t_activity_watch` | None | Allocates `monitor_id` and a new, not-yet-created `activity_file`; returns `state: "waiting"` and descriptive `activity_flag_argv`. |
| `skyn3t_activity_status` | `monitor_id` | Returns observation state, producer-reported outcome, latest stage/model/provider, event/heartbeat/activity ages, counters, and up to 32 recent safe lines. |
| `skyn3t_activity_stop` | `monitor_id` | Stops only the feed; preserves observations for status and explicitly does not cancel SkyN3t. |

Call watch **before** a long SkyN3t run. Pass its returned
`["--activity-file", "<allocated path>"]` argv to the normal `studio improve` or
`studio build` command through Bash, alongside the usual command arguments.
The extension never executes this argv. Bash still owns the command, completion
notification, and authoritative process outcome. A waiting feed is not evidence
that a job started; a stopped or quiet feed is not evidence that a job completed.

The session must expose an absolute `session.workspacePath`. New directories are
created with mode `0700` under its `files/skyn3t-activity/` directory; an existing
activity directory must already be private. No activity file is created by watch:
the producer must use exclusive creation. Tools accept no arbitrary paths.

## Producer contract and limits

Only schema version 1 and the documented `started`, `stage`, `model`, `activity`,
`heartbeat`, `warning`, `completed`, `failed`, and `interrupted` events are accepted.
Records require a stable plain-text `run_id`, increasing positive integer `seq`,
finite nonnegative epoch `timestamp` and `elapsed_s`, and a bounded `message`.
Optional fields are `operation` (`build`/`improve`), `project`, `stage`, `model`,
`provider`, `tool`, relative workspace `path`, nonnegative integer `changed_files`,
and boolean `proof_passed`. Additional fields, including raw prompt, argument,
result, source, and reasoning payload fields, are rejected, not displayed.

Lines are limited to 32 KiB to accommodate JSON-escaped Unicode; messages to 512
Unicode code points; identifiers/project/stage/model/provider/tool to 128; and
paths to 512. Empty optional fields and root-only paths are omitted. Run IDs use
letters, digits, `.`, `_`, `:`, and `-`, starting with a letter or digit. Paths
must be relative without empty, `.` or `..` components, backslashes or colons.
Control/ANSI and directional formatting characters are stripped from display
text. Common credential patterns are rejected across text fields. This is
defense in depth, not a general-purpose secret detector: the producer must still
emit only safe summaries, never raw prompts, source, arguments/results, assistant
text, reasoning, or secrets.

Polling every 500 ms observes only the allocated path and its known parent
directories, without directory enumeration or filesystem event assumptions.
Each poll reads at most 64 KiB in 16 KiB chunks, plus bounded metadata/tail checks.
Byte framing preserves partial lines and split UTF-8. Oversized lines are
discarded through the next newline, and invalid input produces only a generic,
rate-limited warning. Files over 32 MiB stop with a monitor error.

Opening uses `O_NOFOLLOW`; non-regular files and hardlinks are refused. Directory
and file identities are checked before consumption. Removal, replacement,
observed shrinkage, same-size rewrites, and changes to the last 256 consumed
bytes stop monitoring rather than reopen or rewind. These are append-only file
integrity checks, not an audit of every filesystem mutation between polls.

Activity bursts show at most one ordinary activity line per 1.5 seconds, with
the latest pending line retained and flushed. Stage/model/provider changes,
warnings, and terminal records bypass throttling; pending activity is flushed
before them. Heartbeats alone do not produce progress claims. Quiet periods
produce at most one ephemeral, explicitly uncertain liveness line per minute.
Producer-reported terminal outcomes stay distinct from monitor errors and manual
feed stops. Complete queued records are drained before terminal auto-stop;
an incomplete trailing fragment is rejected without being interpreted.

The session retains at most 64 monitor statuses, with at most 8 active feeds.
Both `session.shutdown` and `SIGTERM` remove the observer's listeners, timers,
and file handles. No producer files or processes are removed.

## Local tests

The helpers are independent of the SDK and use Node's built-in test runner:

```text
node --check .github/extensions/skyn3t-activity/extension.mjs
node --check .github/extensions/skyn3t-activity/monitor.mjs
node --check .github/extensions/skyn3t-activity/records.mjs
node --check .github/extensions/skyn3t-activity/tools.mjs
node --check .github/extensions/skyn3t-activity/activity.test.mjs
node --test .github/extensions/skyn3t-activity/activity.test.mjs
```

Tests use only synthetic records and private temporary directories. The SDK is
automatically resolved when Copilot CLI loads `extension.mjs`; do not install it.
