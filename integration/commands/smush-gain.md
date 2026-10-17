---
description: Show smush token savings, with a per-compressor and per-command breakdown
---

Run, using the Bash tool (adding `--session-id "$CLAUDE_SESSION_ID"` if the
user asked for *this session only* rather than all-time):

```
PYTHONPATH="${SMUSH_REPO:-$HOME/repos/smush}" python -m smush gain --breakdown
```

Report the numbers back verbatim — do not re-derive or round them. Then add at
most one sentence of interpretation, leading with the **identity rate**: it is
the share of commands that compressed to nothing, so a high rate means smush is
running but not earning anything, which a healthy-looking total can hide.

If the output says `no compressions recorded`, tell the user compression has
not run in that scope yet and remind them to check `/compress-status` — smush
only logs rows while the mode is `smush`.
