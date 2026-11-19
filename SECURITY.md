# Security policy

Please report vulnerabilities privately using GitHub's
[private vulnerability reporting](https://github.com/kursluzz/reader-assistant/security/advisories/new)
rather than a public issue. Expect an answer within a few days.

Things worth knowing:

- Reader Assistant works offline. It has no account and sends no text anywhere; the only network
  use is the one-time download of voice models, which you do by hand.
- The speech service listens on a Unix socket in `$XDG_RUNTIME_DIR/reader-assistant/`. The directory
  is mode `0700` and the socket is `0600`, so only your user can connect. Any local process running
  as your user can still read what you speak, so treat the machine as the trust boundary.
- The text you read is kept in memory while it is spoken and is not written to the log.
- With `/read-last`, the last Claude Code answer of a session is stored in the same private runtime
  directory (`handoff/`, files `0600`), removed about a day later at the next capture, and cleared at logout when
  `XDG_RUNTIME_DIR` is set.
- The application stores no passwords, API keys or other credentials.
- Only the text you copy or type is read. Images, files and rich text are refused.
