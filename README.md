# Smush

Shrinks command output (JSON, logs and tracebacks, file listings, grep results,
test runs, git output) before it goes into an LLM's context window. The result
is still plain, readable text the model can act on.

- **Stdlib-only Python, no network calls, no telemetry.** The optional MCP
  server is the one piece that needs a third-party package (`mcp`).
- **Reversible by default.** Most codecs restore the original byte-for-byte.
- **Lossy only out loud.** Codecs that discard anything say exactly what they
  dropped, and a test fails the build if one doesn't.

## Why not gzip?

The reader is the model. `H4sIAAAAAAAA/61Ty27...` saves bytes but destroys
the content. smush compresses *structure* instead, so the output stays the same
kind of text. For example, a `find` listing where every line shares a long
prefix:

```
/home/alice/projects/widget/src/module_0/file_0.py
/home/alice/projects/widget/src/module_1/file_1.py
/home/alice/projects/widget/src/module_2/file_2.py
```

becomes

```
~~base /home/alice/projects/widget/src/
module_0/file_0.py
module_1/file_1.py
module_2/file_2.py
```

The prefix is spent once, and the output is still obviously a file listing.

## Quick start

Requires Python 3. Clone the repo; there is nothing to install.

```python
from smush import compress_payload, decompress

result = compress_payload(text, command="git diff")  # command is optional
result.compressed   # send this to the model
result.reduction    # fraction of tokens saved
result.dropped      # what a lossy codec removed (empty if reversible)
original = decompress(result.compressed, result.metadata)  # reversible codecs only
```

Run the tests:

```bash
python -m unittest discover -s tests -v
```

## Codecs

| codec   | handles                                 | reversible? |
|---------|-----------------------------------------|-------------|
| json    | arrays of uniform objects become a `{keys, rows}` table; very long strings are elided | yes |
| log     | folds library traceback frames, collapses repeated lines (digits masked), caps length to head + tail | yes |
| generic | same as log but exact-match only; the safe fallback | yes |
| listing | factors shared path prefixes; caps very long result lists | yes (except `ls -l`, reduced to mode + name) |
| pytest  | keeps failures, skips, errors and the tally; drops progress lines, banners and passing-test names | no |
| git     | one-line commit headers, trims diff context to 2 lines, groups `git status` by state | no |

**Choosing a codec.** If you pass the command that produced the output,
`smush/commands.py` picks the codec from it. Otherwise smush sniffs the content
(json → pytest → git → listing → log → generic → identity). An unrecognized
command returns no hint rather than a guess, because a wrong codec mangles
output while no codec only costs compression.

**Sentinels.** Reversible codecs build their markers (like `~~base`) from a
string they've confirmed is absent from the input, so real data can never be
mistaken for a marker.

**The lossy contract.** A lossy result has `lossy=True`, `reversible=False`,
and a `dropped` list that names each category removed, e.g.
`["12 progress line(s)", "3 library stack frame(s)"]`. Failures, error
messages, paths, SHAs and `+`/`-` diff lines are never dropped.

## CLI

```bash
# compress a file; writes users.json.smush.meta.json, token stats go to stderr
python -m smush compress samples/users.json --stats

# stdin/stdout
cat samples/traceback.log | python -m smush compress --meta-out out.meta.json > out.txt

# reverse it
python -m smush decompress out.txt --meta out.meta.json
```

`compress` options:

- `--type TYPE`: force a codec (default `auto`)
- `--command CMD`: the command that produced the input, used for routing
- `--stats`: before/after token counts on stderr
- `--footer-when-lossy`: when output is lossy, append one line to stdout
  saying what was dropped, e.g.
  `[smush pytest -71% | dropped 4 progress line(s); 16 session/warning header line(s)]`
- `--meta-out PATH`: where to write the metadata needed to decompress
- `--log-stats PATH` and `--session-id ID`: append this call's savings to a
  SQLite ledger

### Tracking savings

```bash
python -m smush gain                         # all-time total
python -m smush gain --session-id <id>       # one session
python -m smush gain --breakdown --top 5     # per codec and per command
python -m smush gain --db path/to/smush_stats.db
```

In `--breakdown`, watch the **identity rate**: the share of calls where no
codec found anything to compress. A high rate means most of your output is
falling through.

## Using it with Claude Code

**MCP server (explicit use).** Exposes `compress`, `decompress` and `stats`
as tools the agent can call. Requires `pip install mcp`.

```bash
claude mcp add smush -- python -m smush.mcp_server
```

**Automatic compression (hook).** The author runs smush as a Claude Code
`PreToolUse` hook on `Bash` that pipes each command's stdout through:

```bash
python -m smush compress --type auto --command "<cmd>" --footer-when-lossy \
  --log-stats <path>/smush_stats.db --session-id <session_id>
```

The hook, a `PostToolUse` hook for `Read` output, and the slash commands are in
[`integration/`](integration/README.md), with install steps. Lessons from
building them, if you adapt or write your own:

- Always pass `--footer-when-lossy`. A hook overwrites its metadata on every
  call, so nothing it compresses can be reversed, and the footer is the only
  way the model learns what's missing.
- Keep stderr out of the pipe. Error text must never be summarized away, and
  mixing it in breaks content detection.
- Fail open. Any error should pass the original command through untouched.
- Skip commands containing heredocs (`<<`). Wrapping them in a pipe produces
  a bash syntax error.
- Keep a leading `cd <dir> &&` outside the wrapped part so the directory
  change still persists.
- Claude Code hands every `PreToolUse` hook the original input, and only one
  hook's `updatedInput` survives. If another hook also rewrites Bash commands,
  combine them into one hook.

## Measured results

`python -m smush.harness` runs every codec over `samples/` (or a directory you
pass) and exits non-zero if any reversible codec fails to round-trip:

| file             | codec   | reduction | round-trip |
|------------------|---------|-----------|------------|
| grep_results.txt | listing | 18.9%     | OK         |
| git_status.txt   | git     | 38.4%     | lossy      |
| users.json       | json    | 41.1%     | OK         |
| git_diff.txt     | git     | 54.3%     | lossy      |
| ls_long.txt      | listing | 56.9%     | lossy      |
| traceback.log    | log     | 57.2%     | OK         |
| pytest_run.txt   | pytest  | 70.8%     | lossy      |

Average: 48.2%.

**Treat that as an upper bound.** The samples were written by the same person
who wrote the codecs. On real captured output from ordinary commands
(`python scripts/capture_corpus.py`), the same codecs save **15%**. Most of
that real-world volume is `git diff` (2% saved) and `ls -R` (0%; its block
format isn't parsed yet), so those are the next things to improve.

Token counts come from `smush/tokens.py`, a regex-based estimator calibrated
against two real tokenizers. It's used only for before/after ratios.

## Privacy

`smush_stats.db` stores every compressed command **verbatim**, including paths,
branch names and anything else on the command line. It is gitignored; keep it
that way. Likewise `corpus/` holds real machine output and is gitignored.
`samples/` is synthetic, so anyone can reproduce the published numbers.

## Repo layout

- `smush/core.py`: `compress_payload()`, routing and the codec registry
- `smush/commands.py`: command → codec hint
- `smush/result.py`: `CompressResult`
- `smush/tokens.py`: token estimator
- `smush/compressors/`: one file per codec
- `smush/cli.py`, `smush/harness.py`, `smush/mcp_server.py`, `smush/stats_store.py`
- `scripts/`: corpus capture, Ollama demo, chat REPL
- `tests/`: stdlib `unittest`

## License

Apache 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
