---
description: Switch Bash output compression to smush
---

Write the single word `smush` into `~/.claude/.compress_mode`, replacing whatever is there, using the Bash tool: `printf 'smush\n' > "$HOME/.claude/.compress_mode"`.

Then tell the user in one short sentence: compression mode is now smush (rtk will not run).
