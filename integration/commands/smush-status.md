---
description: Show which Bash output compressor is currently active — alias for /compress-status
---

Read `~/.claude/.compress_mode` using the Bash tool: `cat "$HOME/.claude/.compress_mode" 2>/dev/null || echo "(missing)"`.

Report the result to the user in one short sentence, interpreting it:

- `smush` — smush compresses Bash output; rtk does not run
- `rtk` — rtk rewrites Bash commands; smush does not run
- `off` — neither runs; output is raw
- `(missing)` or anything unrecognized — treated as `rtk` by the hook
