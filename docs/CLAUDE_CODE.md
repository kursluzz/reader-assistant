# Reading Claude Code answers aloud

`/read-last` reads the last assistant message of the current Claude Code session through the running Reader Assistant service. Nothing is spoken automatically.

## How it works

1. A **Stop hook** runs after each Claude Code response. It stores the response's final text in a private file for that session (`capture-last`). It never speaks, and it never blocks Claude.
2. **`/read-last`** runs `reader-assistant read-last`. It reads the stored text for the current session (`CLAUDE_CODE_SESSION_ID`), cleans the Markdown, splits long text into parts of at most 4,800 UTF-8 bytes, and speaks the parts in order. The next part starts when the previous one has finished.
3. The reply to `/read-last` itself is not stored. The command leaves a short-lived marker that makes the next capture for the same session skip one response. The marker is set again when the command finishes, so a long reading cannot make it expire before the Stop hook runs. The stored answer stays available for reading again.

Checked with Claude Code 2.1: the Stop hook receives `session_id`, `transcript_path` and `last_assistant_message`, and the same session ID is available to commands as `CLAUDE_CODE_SESSION_ID`. The Stop hook also fires after a slash command, which is why the skip marker exists.

## Setup

1. Install and start the service (see the [README](../README.md)): `uv run reader-assistant serve`.
2. Install the `/read-last` command for your user, so that it works in every project:

   ```bash
   mkdir -p ~/.claude/commands
   cat > ~/.claude/commands/read-last.md <<'EOF2'
   ---
   description: Read the last assistant message of this session aloud with Reader Assistant
   allowed-tools: Bash(/absolute/path/to/reader-assistant/.venv/bin/reader-assistant read-last)
   ---
   !`/absolute/path/to/reader-assistant/.venv/bin/reader-assistant read-last`

   Reply with only the word "Done." and nothing else.
   EOF2
   ```

   Replace `/absolute/path/to/reader-assistant` with your clone's path.

3. Add the Stop hook to your user settings, `~/.claude/settings.json`. It applies to every project. Replace the path with your clone's `.venv` binary:

   ```json
   {
     "hooks": {
       "Stop": [
         {
           "hooks": [
             {
               "type": "command",
               "command": "/absolute/path/to/reader-assistant/.venv/bin/reader-assistant capture-last",
               "timeout": 20
             }
           ]
         }
       ]
     }
   }
   ```

4. In any Claude Code session, ask a question, then type `/read-last`.

## Privacy

- The stored text is written only to the service's private runtime directory (`$XDG_RUNTIME_DIR/reader-assistant/handoff/`, mode 0700, files 0600). Messages older than 24 hours are removed at the next capture, so the removal is not exact to the hour. The directory is also cleared at logout when `XDG_RUNTIME_DIR` is set.
- The text is not logged, and it is sent only to the local service socket.
- Each session has its own file. A command never reads another session's message.

## Exit codes of `read-last`

| Code | Meaning |
|---:|---|
| 0 | The message was read, or reading was stopped by another request (for example a shortcut press). |
| 1 | No session, no stored message, nothing to read aloud, or speech failed. |
| 3 | The speech service is not running. |

`capture-last` always exits with 0 and prints nothing. It does not read the configuration, so a broken configuration file cannot make it fail.

## Limits

- Only the **final** text of each response is stored. Tool output and reasoning are not.
- Code blocks are left out. Inline code is read as plain words.
- A message is stored per session. Only the latest response of a session can be read.
- Both the hook and the command are user-wide. The application does not change Claude Code's settings itself; the setup above is done by hand.
- The command uses the absolute path to the binary, so it works from any directory.
