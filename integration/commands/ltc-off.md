---
description: Turn off ltc (falls back to rtk) — alias for /rtk-on
---

Historically this meant "stop using ltc", which in practice left rtk handling commands. Preserve that meaning: write the single word `rtk` into `~/.claude/.compress_mode` using the Bash tool: `printf 'rtk\n' > "$HOME/.claude/.compress_mode"`.

Then tell the user in one short sentence: ltc is off, compression mode is now rtk — and mention that `/compress-off` turns off compression entirely if that is what they wanted.
