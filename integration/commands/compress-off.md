---
description: Turn off all Bash output compression (neither smush nor rtk)
---

Write the single word `off` into `~/.claude/.compress_mode`, replacing whatever is there, using the Bash tool: `printf 'off\n' > "$HOME/.claude/.compress_mode"`.

Then tell the user in one short sentence: compression is now off — Bash output is passed through raw.
