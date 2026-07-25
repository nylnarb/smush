---
description: Switch Bash output compression to ltc
---

Write the single word `ltc` into `~/.claude/.compress_mode`, replacing whatever is there, using the Bash tool: `printf 'ltc\n' > "$HOME/.claude/.compress_mode"`.

Then tell the user in one short sentence: compression mode is now ltc (rtk will not run).
