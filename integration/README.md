# `integration/` — the Claude Code wiring for `ltc`

Everything in this directory lives *outside* the repo when it is installed: it
gets copied into your Claude Code home directory (`~/.claude/`, i.e.
`%USERPROFILE%\.claude\` on Windows). That directory is not version controlled,
so these are the canonical copies. If a machine dies, reinstall from here.

Nothing here is imported by the `ltc` package. `ltc` works standalone from the
CLI; this directory is only the glue that makes it run automatically inside
Claude Code.

Throughout this document:

| placeholder | meaning |
|---|---|
| `<CLAUDE_HOME>` | your Claude Code config dir — `~/.claude`, on Windows `%USERPROFILE%\.claude` |
| `<LTC_REPO>` | your clone of this repository |
| `<PYTHON>` | the interpreter that should run `ltc` (3.8+, stdlib only) |

The vendored files contain **no absolute machine paths** — they derive
everything from `$HOME` / `~` and accept environment-variable overrides. That is
deliberate: this repository is public.

---

## Contents

### `hooks/ltc-auto-compress.py`

The single `PreToolUse` hook registered for the `Bash` tool. It is a
**dispatcher**, not a compressor: it reads one word from
`<CLAUDE_HOME>/.compress_mode` and routes accordingly.

| mode | behavior |
|------|----------|
| `ltc` | rewrites the outgoing command so its stdout is piped through `python -m ltc compress`; rtk does not run |
| `rtk` | forwards the identical hook payload to `headroom-rtk-rewrite.py` and echoes that verdict verbatim; ltc does not run |
| `off`  | does nothing; the command runs unmodified |
| *(missing / unreadable / unrecognized)* | treated as `rtk` |

Key behaviours, all of which exist because of a bug that was actually hit:

- **Fails open, always.** Any exception, unknown mode, missing binary, or
  non-rewritable command exits 0 with no stdout, which Claude Code reads as
  "run the original command unchanged". A hook must never be able to block a
  command.
- **A leading `cd` is hoisted, not skipped.** `cd <dir> && <cmd>` becomes
  `cd <dir> && ( <cmd> ) | ltc compress …`, so the directory change still runs
  in the parent shell and persists into later Bash calls, while only `<cmd>` is
  compressed. `cd ` used to sit in the skip list, and since that list is tested
  with `startswith` against the *whole* command, every `cd <repo> && <anything>`
  was exempted outright — 38 of 55 Bash calls in one measured session.
- **Heredocs are skipped entirely.** `python - <<'PY' … PY` wrapped as
  `( … ) 2>file | ltc; cat file` is a bash syntax error. Detection is a crude
  `"<<" in cmd`: a false positive costs one uncompressed command, a false
  negative breaks the command.
- **stderr is kept out of the pipe.** Mixing diagnostics into the payload breaks
  the content-type detection, and error text is exactly what must never be
  summarized away.

#### Why exactly ONE hook per event

Claude Code hands *every* `PreToolUse` hook the **original** `tool_input`. When
two hooks both emit `updatedInput`, whichever is listed first wins and the
other's work is silently discarded — no error, no warning, and no switch file
can change it. Registering `ltc`'s hook and `rtk`'s hook side by side therefore
cannot work; one of them is always a no-op. That cost a full debugging session
before it was understood.

The single-dispatcher-plus-mode-file design exists specifically to prevent this:
one registered hook, one mode file, and the delegation to rtk happens as a
subprocess call *inside* the dispatcher rather than as a second registration.

The rule is **per event**, not global. A `PostToolUse` hook alongside this
`PreToolUse` one is fine; two `PreToolUse` hooks that both rewrite input are not.

### `commands/*.md`

Claude Code slash commands. Each is a markdown file whose name is the command
name; the YAML front matter's `description` is what shows in the picker.

| file | command | what it does |
|---|---|---|
| `ltc-on.md` | `/ltc-on` | writes `ltc` to the mode file |
| `rtk-on.md` | `/rtk-on` | writes `rtk` to the mode file |
| `compress-off.md` | `/compress-off` | writes `off` to the mode file |
| `compress-status.md` | `/compress-status` | reads and interprets the mode file |
| `ltc-status.md` | `/ltc-status` | alias for `/compress-status` |
| `ltc-off.md` | `/ltc-off` | alias for `/rtk-on` (historical meaning: "stop using ltc", which left rtk running) |
| `ltc-gain.md` | `/ltc-gain` | runs `python -m ltc gain --breakdown` and reports savings, leading with the identity rate |

`/ltc-gain` reads `PYTHONPATH` from `$LTC_REPO`, falling back to
`$HOME/repos/ln_token_compressor`. If your clone lives elsewhere, either export
`LTC_REPO` or edit that one line after installing.

---

## Installing on a fresh machine

1. **Clone this repo** somewhere; call that path `<LTC_REPO>`. There is nothing
   to build and no dependencies — `ltc` is stdlib-only.

   Sanity check:

   ```bash
   PYTHONPATH="<LTC_REPO>" <PYTHON> -m ltc --help
   ```

2. **Copy the hook** into `<CLAUDE_HOME>/hooks/`:

   ```bash
   mkdir -p ~/.claude/hooks
   cp <LTC_REPO>/integration/hooks/ltc-auto-compress.py ~/.claude/hooks/
   ```

3. **Copy the slash commands** into `<CLAUDE_HOME>/commands/`:

   ```bash
   mkdir -p ~/.claude/commands
   cp <LTC_REPO>/integration/commands/*.md ~/.claude/commands/
   ```

4. **Register the hook** in `<CLAUDE_HOME>/settings.json`. Add (or merge into)
   the `hooks` block — substituting your real `<PYTHON>` and `<CLAUDE_HOME>`,
   and remembering that JSON needs backslashes doubled on Windows:

   ```json
   {
     "hooks": {
       "PreToolUse": [
         {
           "matcher": "Bash",
           "hooks": [
             {
               "type": "command",
               "command": "\"<PYTHON>\" \"<CLAUDE_HOME>/hooks/ltc-auto-compress.py\"",
               "timeout": 20
             }
           ]
         }
       ]
     }
   }
   ```

   **Make sure this is the only entry under `PreToolUse` with `"matcher": "Bash"`
   that rewrites the command.** See "Why exactly ONE hook per event" above. If
   you already have rtk's hook registered, remove that registration — the
   dispatcher calls it for you in `rtk` mode.

5. **Set the mode.** The hook defaults to `rtk` when the file is absent, so to
   actually use ltc:

   ```bash
   printf 'ltc\n' > ~/.claude/.compress_mode
   ```

   or just run `/ltc-on` in a session. Verify with `/compress-status`.

6. **Optional environment overrides.** If your layout differs from the defaults,
   set any of these (in `settings.json`'s `env` block, or in the shell):

   | var | default | purpose |
   |---|---|---|
   | `LTC_CLAUDE_DIR` | `~/.claude` | where the mode file, log, and rtk hook live |
   | `LTC_PYTHON` | the interpreter running the hook | interpreter used to run `ltc` and delegate to rtk |
   | `LTC_REPO` | `~/repos/ln_token_compressor` | clone location; also where `ltc_stats.db` is written |

   Setting these is preferred over editing the hook, so future re-vendors stay
   drop-in.

## Verifying it works

- `/compress-status` should report `ltc`.
- Run any Bash command, then check the dispatcher log:
  `<CLAUDE_HOME>/hooks/ltc-auto-compress.log`. Each line is either
  `LTC | <command>` (wrapped) or `passthrough: skipped (…)`. A log dominated by
  `skipped` lines is the first thing to check when savings look low.
- `/ltc-gain` should show non-zero rows once a few commands have run.

## Notes

- `ltc_stats.db` (written to `<LTC_REPO>/ltc_stats.db`) is the savings ledger.
  It is gitignored and must never be committed — it contains command text from
  real sessions.
- `rtk` mode requires `headroom-rtk-rewrite.py` to be present in
  `<CLAUDE_HOME>/hooks/`. If it isn't, delegation fails and the command passes
  through uncompressed — safe, just not useful. That file belongs to rtk and is
  not vendored here.
- The statusline script described in the top-level README is not vendored; it is
  optional and independent of this hook.
