# ln_token_compressor (ltc)

A dependency-free (stdlib-only) Python toolkit that shrinks large text blobs —
JSON payloads, logs/tracebacks, file listings/search results, test runs and git
output — before they enter an LLM context window. Most codecs reverse the
transform exactly; the command-aware ones trade that for a much larger
reduction and declare precisely what they dropped.

Current status: **Layer 1 (core library + compressors), Layer 2 (MCP server,
CLI, measurement harness) and all four stages (A–D) of the Claude Code
integration are done — 174 passing tests.** The codecs average 47.2%
reduction across `samples/`, and every reversible one round-trips
byte-for-byte under test.

Two commitments shape the whole design and are worth knowing up front:
compression is **reversible by default**, and any codec that discards
something must declare exactly what, in a footer the caller cannot miss (see
[Key idea](#key-idea-reversible-by-default-lossy-only-out-loud)). The core
package is stdlib-only and makes **no network calls** — which is what lets it
run on a managed machine at all.

New to any of this? Start with the
[crash course](#crash-course-what-this-thing-is-and-why-it-exists) directly
below — it assumes you can write code but have never touched an LLM.

## Crash course: what this thing is, and why it exists

*Written for someone who's had a couple of CS courses and has never worked on
an LLM. If you already know what a context window is, skip to
[How to read this repo](#how-to-read-this-repo-suggested-order).*

### 1. Models read tokens, and tokens are a budget

A language model doesn't read characters or words. Text gets chopped into
**tokens** — roughly 3–4 characters each, so `"listing_compressor"` might be
`listing`, `_`, `comp`, `ressor`. Rule of thumb: **1 token ≈ 4 characters** of
ordinary English or code.

That matters because a model has a **context window**: a hard cap on how many
tokens it can consider at once. Everything lives in there together — the system
prompt, your messages, the model's replies, and crucially *every byte of output
from every command it runs*.

Two consequences that drive this entire project:

- **It's a fixed budget, not a stream.** Fill it and something has to go. In a
  coding agent, that means the model starts forgetting what you told it an hour
  ago because a `find` command dumped 9,000 tokens of `.git/` internals into the
  window.
- **You pay for it, repeatedly.** The whole window is re-sent on every single
  turn. A wasteful 5,000-token blob isn't paid for once; it's paid for on every
  subsequent message in the conversation.

So: fewer tokens = longer memory, lower cost, faster responses. That's the
entire value proposition. `ltc/tokens.py` is where this repo counts them —
note it's a *heuristic estimator* (a regex, not a real tokenizer), calibrated
against two real BPE tokenizers offline and held to a stated error band by
`tests/test_tokens_calibration.py`. It only ever computes before/after ratios,
so being within a few percent is fine.

### 2. Why you can't just use gzip

This is the part that surprises people, and it's the single most important idea
in the repo.

The obvious move is `gzip`. It doesn't work, and not because of a technical
limitation — because of who the reader is. **The consumer of this text is the
model itself.** Compressed output has to remain *text the model can read and act
on*. Hand it `H4sIAAAAAAAA/61Ty27...` and you haven't saved anything; you've
destroyed the thing you were trying to deliver.

So every transform here obeys a constraint gzip never has to:

> The output must stay valid, readable output of the same kind as the input.

That rules out entropy coding entirely and pushes you toward **structural**
compression — finding *redundancy specific to the shape of the data* and
factoring it out in a way that still reads as a file listing, a log, a diff.

Concretely, here's `listing_compressor.py` on a `find`:

```
/home/alice/projects/widget/src/module_0/file_0.py     ← the same 38-character
/home/alice/projects/widget/src/module_1/file_1.py       prefix, 10 times over
/home/alice/projects/widget/src/module_2/file_2.py
...
```

becomes

```
~~base /home/alice/projects/widget/src/
module_0/file_0.py
module_1/file_1.py
module_2/file_2.py
```

Same information, **50% fewer tokens** (measured, not estimated), and still
obviously a file listing. That's the whole game: **spend the shared part once.**

`json_compressor.py` plays the same trick on a different axis. An array of 100
uniform objects repeats every key 100 times, so it gets pivoted into a columnar
`{keys: [...], rows: [[...]]}` table — keys once, values as bare rows.

### 3. Sentinels, or: how not to corrupt the data

Notice that `~~base` marker above. It's an instruction injected *into* the data
stream, which raises an obvious bug: what if the real input already contains a
line starting with `~~base`? Decompression would mistake real content for a
marker and mangle the file.

The fix is a **sentinel** — before compressing, scan the input and pick a short
string *provably absent from it* (`~~`, else `~~0`, `~~1`, … until one doesn't
occur), then build every marker from that. Collisions become impossible rather
than unlikely. `_pick_sentinel()` appears in each reversible codec, and
`test_sentinel_collision_avoided` feeds the codec input containing a literal
`~~base` line to prove it.

This is a good habit to steal generally: when you inject metadata into a data
stream, derive your delimiter *from the data* instead of hoping your magic
constant never shows up.

### 4. Lossless vs. lossy — and why "lossy" is a contract, not an excuse

Some transforms are perfectly reversible. Prefix factoring is: keep the prefix
in a `metadata` dict and `decompress()` reproduces the original **byte for
byte**. The tests assert exactly that: `decompress(compress(x)) == x`.

But the biggest wins aren't reversible, because a lot of command output is pure
ceremony. `git status` prints `(use "git restore <file>..." to discard...)`.
`pytest -v` prints a line for every *passing* test. No clever encoding of
ceremony beats **not sending it at all**.

The danger is obvious: a compressor that silently deletes things is a
compressor that lies to you. So lossy codecs here trade reversibility for a
different, enforced contract:

- `result.reversible` is `False` and `result.lossy` is `True`;
- `result.dropped` **names what went** — not "some lines", but
  `"176 entries under .git/"`;
- `tests/test_harness.py::LossyContractTests` walks every registered codec and
  fails the build if anything is lossy with an empty `dropped`. You *cannot*
  merge a codec that discards silently.

The design rule, and it's a good one for any destructive operation you build:
**being lossy is fine; being lossy and quiet is not.**

### 5. Where the tokens actually are (measure first)

Now the part that separates this from a toy. You could spend a month tuning
codecs and move nothing, so: where does a real session's context actually go?
Measured on one full session:

| source | share | can ltc touch it? |
|--------|-------|-------------------|
| assistant messages | 38% | no |
| **`Read` tool output** | **37%** | **not yet — reachable, unbuilt** |
| **`Bash` tool output** | **20%** | **yes — this is all ltc does today** |
| Edit / Write / Grep | 4% | not yet |

ltc is installed as a **hook** — a script the agent runs automatically at a
fixed point in its lifecycle. Specifically a `PreToolUse` hook on `Bash`: before
a shell command runs, ltc gets to rewrite it (piping the output through itself).

That placement is the ceiling on everything ltc currently does. **Even a perfect
compressor — 100% reduction, every command — could only reclaim 20% of the
window.** The biggest single target is `Read` output at 37%, and ltc does not
touch a byte of it.

For most of this project's life the README said that was permanent: hooks can
rewrite tool *input*, never tool *output*. That was verified in the shipped
binary rather than guessed — and **it is no longer true.** As of Claude Code
2.1.220, `PostToolUse` accepts `updatedToolOutput`, documented in the binary as
*"Replaces the tool output before it is sent to the model"*, and the dispatch
loop genuinely consumes it. See [Reach: what changed](#reach-what-changed) for
the mechanism and its one sharp edge. Nothing in ltc uses it yet, so every
measured number on this page is still a Bash-only number.

Two lessons, and the second only exists because the first was written down:

- **This was optimized as a compression problem when it is substantially a
  placement problem.** Find out where the volume is before you tune the thing
  you already know how to tune.
- **A ceiling you verified is still only true of the version you verified.**
  "Checked the binary" beat "assumed", and then a release moved the wall. The
  note that saved this was the one that said *re-verify after any upgrade*.

### 6. How a codec gets chosen — and why the command matters more than the bytes

Given a blob of text, which codec runs? The naive answer is sniff it — does it
start with `{`, does it contain `Traceback`, do most lines look like paths?
`_sniff()` in `core.py` does exactly that, as a **fallback**.

The primary signal is better: **what command produced it.** `ltc/commands.py`
maps `git diff` → the git codec, `pytest` → the pytest codec, `find` → listing.
This wins because content sniffing has a blind spot it can never fix — an
all-passing test run looks nothing like a traceback, contains no failure text,
and still wants the pytest codec.

`classify()` returns `None` rather than guessing when it doesn't recognize a
command, and that asymmetry is deliberate: a *wrong* hint routes output to a
codec that will mangle it, while `None` just costs you some compression.
**When the failure modes are lopsided, bias toward the cheap one.**

### 7. Two measurement lessons this repo learned the hard way

Both are recorded rather than quietly fixed, because they're the genuinely
transferable part.

**Your benchmark will flatter you.** The codecs scored 47.2% on `samples/` —
fixtures written by the same person who wrote the codecs. On `corpus/`, real
captured command output, the same codecs scored **15%**. Not a bug: a benchmark
built by the implementer tests what the implementer already thought of. The
fix was to build a second corpus from ordinary commands *not chosen for how
well they compress*, and treat that as the real number. There is now a standing
rule that you may not add a fixture to `samples/` to make a result look better.

**A healthy metric can move while the thing it stands for doesn't.** One change
drove the "identity rate" (share of payloads compressed to nothing) from 100%
to 0% — an unambiguous win, except savings didn't budge. Content now *reached*
a codec, which then did nothing with it. The proxy improved; the goal didn't.

### 8. The idea that actually moved the needle

The most recent finding ties all of the above together. A comparable tool saved
~92% on a recursive `find` where ltc saved 15%. Decomposing that win showed
**86.7% of it wasn't compression at all** — it was *filtering*: silently
dropping `.git/`, `__pycache__/`, `node_modules/`. Only the remainder came from
smarter encoding.

The gap was never codec quality. It was **permission to discard**, which ltc's
round-trip contract had never granted. Same 293-file input, after granting it:
9,036 tokens → 363, **96%**, with zero real files dropped.

But look at what "permission" has to mean, per §4 — the filter is gated so that
if the command *names* the directory (`ls .git/`, `find . -path '*node_modules*'`),
nothing is filtered, because then the user asked for exactly that and hiding it
isn't compression, it's a wrong answer. With no command hint at all it doesn't
filter anything. And every drop is counted, named, and announced in the output
itself.

That's the shape of the whole project in one feature: **the aggressive move was
available the entire time; what made it safe to take was being forced to say
out loud what it cost.**

## How to read this repo (suggested order)

1. **`ltc/__init__.py`** — the design contract in one docstring: transport-agnostic,
   no network, no global state, pure functions. Read this first; everything else
   follows from these constraints.
2. **`ltc/core.py`** — the single entry point,
   `compress_payload(content, content_type, command)`. Also holds `_resolve_auto()`
   (command hint first, content sniffing second), `_sniff()`
   (json/pytest/git/listing/log/generic/identity) and the `_REGISTRY` dict mapping
   a content type to its compress/decompress pair.
3. **`ltc/commands.py`** — `classify(command)`, mapping a shell command string to a
   content-type hint. Returns `None` rather than guessing: a wrong hint routes
   output to a codec that will mangle it, while `None` only costs compression.
4. **`ltc/result.py`** — `CompressResult`, the frozen dataclass every compressor
   returns. Carries the compressed text plus `metadata` (everything needed to
   reverse it), token counts, derived properties (`.reduction`, `.tokens_saved`,
   `.lossy`), and `dropped` — the list of what a lossy transform threw away.
5. **`ltc/tokens.py`** — `estimate_tokens()`, a heuristic (regex-based, no real
   tokenizer/model) used only to compute before/after ratios. Its constants are
   fitted against two real BPE tokenizers offline and held to a stated error
   band by `tests/test_tokens_calibration.py`.
6. **`ltc/compressors/json_compressor.py`** — the biggest win: collapses arrays of
   uniform objects into a columnar `{keys, rows}` table instead of repeating keys
   per row. Also elides very long strings. Fully reversible via a `~~`-prefixed
   sentinel guaranteed absent from the input.
7. **`ltc/compressors/log_compressor.py`** — three chained transforms: fold
   library/third-party traceback frames, collapse runs of repeated lines, and cap
   very long output to head+tail. Exact byte-for-byte reversible via stored ops.
   Exposes two entry points: `compress` (digit-masked run matching, for real logs)
   and `compress_generic` (exact matching, the safe catch-all — see
   [Content-type routing](#content-type-routing)).
8. **`ltc/compressors/listing_compressor.py`** — file listings / search results:
   factors a shared directory prefix into one header line (handles both plain
   `ls`-style and `path:line:text` grep-style lines; requires every kept line to
   be path-shaped, otherwise it's a no-op), and caps result counts past a
   threshold with an "N results omitted" marker — both exactly reversible. Adds
   one **lossy** transform that fires only on `ls -l` long format, reducing each
   line to mode + name.
9. **`ltc/compressors/pytest_compressor.py`** — **lossy.** Keeps failures, the
   short summary lines and the final tally; drops the session banner, progress
   lines, `-v` passing-test names (`... ok` / `PASSED`), the warnings summary
   and library stack frames. Skips and errors are kept — a skipped test is one
   that did not run, which a reader may need to know.
10. **`ltc/compressors/git_compressor.py`** — **lossy.** Three independent
   transforms, each a no-op on content lacking its shape, so `git log -p`
   (genuinely both a log and a diff) works: fold commit headers to one line,
   trim unchanged diff context to two lines around each change, and group
   `git status` paths by state while dropping the `(use "git ...")` hints.
11. **`tests/`** — stdlib `unittest` tests. Reversible codecs are held to
   `decompress(compress(x)) == x`; lossy codecs are held to a different
   contract — nothing load-bearing removed, and everything removed declared —
   plus CLI and harness tests.
12. **`ltc/cli.py`** — stdlib-only command-line interface (`python -m ltc`).
13. **`ltc/harness.py`** — measurement harness that runs every registered
   compressor over `samples/` and reports a reduction table.

## Key idea: reversible by default, lossy only out loud

There are two classes of codec and a result always says which one it is.

**Reversible (json, log, generic, listing).** Each picks a "sentinel" string
that provably does not occur in the input and builds all its in-band markers
from that sentinel. Marker collisions with real data are therefore impossible,
so `decompress()` undoes the transform exactly using only the compressed text +
its `metadata` dict.

**Lossy (pytest, git, and listing's `ls -l` column elision).** Some output is
mostly ceremony — progress dots, `(use "git restore ...")` hints, three lines
of unchanged diff context — and no reversible encoding of ceremony beats simply
not sending it. These codecs discard it and cannot restore it: `decompress()`
returns its input unchanged. The contract that replaces reversibility:

- `result.reversible` is `False` and `result.lossy` is `True`;
- `result.dropped` names every category removed, e.g.
  `["12 progress line(s)", "3 library stack frame(s)"]`;
- nothing load-bearing ever goes — no failure, error message, path, sha, or
  `+`/`-` diff line — only material a reader reconstructs for free or would
  re-run the command to see;
- a codec that finds nothing of its own shape returns an identity result rather
  than claiming a compression it didn't make.

The measurement harness enforces the split: it requires an exact round-trip
from anything claiming `reversible=True`, and exempts (and labels) the rest.

## Quick usage

```python
from ltc import compress_payload, decompress

result = compress_payload(some_json_or_log_text)   # content_type="auto" by default
result.compressed        # what you'd send to the LLM
result.metadata          # keep this if you need to reverse later
result.reduction         # fraction of tokens saved
original = decompress(result.compressed, result.metadata)
```

## Running tests

```bash
python -m unittest discover -s tests -v
```

## CLI

```bash
# compress a file, writing a metadata sidecar next to it
python -m ltc compress samples/users.json --stats
# -> writes samples/users.json.ltc.meta.json, prints compressed text to stdout,
#    prints token stats to stderr

# compress via stdin/stdout
cat samples/traceback.log | python -m ltc compress --meta-out out.meta.json > out.txt

# reverse it (needs the compressed text + its metadata sidecar)
python -m ltc decompress out.txt --meta out.meta.json
```

`compress` accepts `--type` (one of `SUPPORTED_TYPES`, default `auto`),
`--stats` (prints before/after token counts and reduction % to stderr, keeping
stdout a clean pipe of the compressed text), `--footer-when-lossy` (see
[the lossy footer](#the-lossy-footer)), `--meta-out` (defaults to
`<file>.ltc.meta.json`, or `stdin.ltc.meta.json` for piped input), `--log-stats
PATH` (append this call's savings to a SQLite ledger, see below), and
`--session-id ID` (tag that ledger row with an id, so savings can later be
queried per-session). `decompress` requires `--meta` pointing at that sidecar.

### The lossy footer

`--footer-when-lossy` appends exactly one line to **stdout** when — and only
when — the result is lossy:

```bash
python -m ltc compress samples/pytest_run.txt --footer-when-lossy
# ...compressed output...
# [ltc pytest -71% | dropped 4 progress line(s); 16 session/warning header line(s)]

python -m ltc compress samples/git_status.txt --command "git status" --footer-when-lossy
# [ltc git -38% | dropped 4 git hint line(s); 9 path(s) grouped onto shared lines]
```

Three deliberate choices:

- **stdout, not stderr.** Unlike `--stats`, this line is not diagnostics for a
  human watching a terminal — it must land in the same text the model reads,
  directly under the output it describes.
- **Silent when reversible.** A line on every command would cost tokens on the
  overwhelmingly common case and would train a reader to skip past exactly the
  line that matters.
- **It annotates, never replaces.** The footer is additive; the failure it
  summarizes is still in the output above it (`tests/test_cli.py` asserts
  this).

This is the whole safety mechanism for lossy auto-compression, because the
hook points `--meta-out` at a single file that every call overwrites — nothing
compressed through that path is reversible in practice, so being *told* what is
missing is the only protection a reader has. The hook therefore always passes
this flag.

### Tracking savings over time: `ltc gain`

Every `ltc compress --log-stats PATH` call appends one row (timestamp,
compressor, before/after token counts, `session_id`, the originating `command`,
and whether the result was `lossy`/`reversible`) to a SQLite ledger at `PATH`.
Query it with:

```bash
# all-time total across every session
python -m ltc gain
# -> 184213 tokens saved (all-time)

# total for one session only
python -m ltc gain --session-id <session-id>
# -> 4210 tokens saved (session <session-id>)

# point at a specific ledger file instead of the default ltc_stats.db
python -m ltc gain --db path/to/ltc_stats.db
```

Nothing is ever deleted or aggregated in place — the raw per-call history is
always available for re-querying.

#### `--breakdown`: is ltc actually working?

A total can look healthy while most commands quietly fall through every codec
and save nothing. `--breakdown` surfaces that directly:

```bash
python -m ltc gain --breakdown            # all-time
python -m ltc gain --breakdown --session-id <id> --top 5
```

```
ltc savings (all-time)
  928 tokens saved of 5071 (18%) across 24 commands
  identity rate: 67% (16 of 24 compressed to nothing)
  lossy calls:   0

  by compressor
    listing       5 calls       777 saved    28%
    pytest        1 calls       151 saved    75%
    identity     16 calls         0 saved     0%

  top 3 commands by savings
    ls -la               5 calls       777 saved    28%
    pytest -q            1 calls       151 saved    75%
    git status           2 calls         0 saved     0%
```

**Identity rate is the health metric.** Commands are grouped by their first two
words, which keeps `git status` apart from `git diff` while collapsing the paths
and flags that differ on every run. Rows recorded before the `command` column
existed group under `(unknown)`.

## Claude Code integration: auto-compress hook + statusline

Two pieces outside this repo (in `~/.claude/`) wire `ltc` into Claude Code
automatically, governed by a single **mode file**, `~/.claude/.compress_mode`,
containing exactly one word:

| mode | behavior |
|------|----------|
| `ltc` | ltc compresses Bash output; rtk does not run |
| `rtk` | rtk rewrites Bash commands; ltc does not run |
| `off` | neither runs; output passes through raw |
| *(missing/unrecognized)* | treated as `rtk` |

Set it with `/ltc-on`, `/rtk-on`, `/compress-off`; inspect with
`/compress-status`; report savings with `/ltc-gain`. (`/ltc-off` and
`/ltc-status` remain as aliases.)

- **`~/.claude/hooks/ltc-auto-compress.py`** — the *only* `PreToolUse` hook
  registered for the `Bash` tool, acting as a dispatcher. In `ltc` mode it
  rewrites the outgoing command to pipe its stdout through `python -m ltc
  compress --type auto --command <cmd> --footer-when-lossy --log-stats
  <repo>/ltc_stats.db --session-id <session_id>`. In `rtk` mode it hands the
  identical payload to
  `headroom-rtk-rewrite.py` and forwards that verdict verbatim. Fails open:
  any error, unknown mode, or skip-listed command (`claude `, `ollama run`, a
  bare `cd`, anything containing a heredoc) results in silent passthrough — it
  can never block a command.

  **A leading `cd` is hoisted, not skipped.** `cd <dir> && <cmd>` becomes
  `cd <dir> && ( <cmd> ) | ltc compress …` — the `cd` stays *outside* the
  subshell so its directory change still persists into later Bash calls, and
  only `<cmd>` is compressed. This matters more than it sounds: `cd ` used to
  sit in the skip list, and since that list is tested with `startswith` against
  the whole command, every `cd <repo> && <anything>` — which is how most
  commands in a repo session are written — was exempted outright. Measured on
  one real session: **38 of 55 Bash calls skipped**, ltc saw 2. The `--command`
  hint now also gets the real command rather than `cd`, so
  [routing](#content-type-routing) improves for free.

  **Heredocs are skipped entirely.** `python - <<'PY' … PY` wrapped as
  `( … ) 2>file | ltc; cat file` is a bash syntax error — the redirection and
  `;` trailer land between the heredoc's opening line and its body. Detection
  is a crude `"<<" in cmd`: a false positive costs one uncompressed command, a
  false negative breaks the command outright.

  **Why one hook and not two.** Claude Code hands every `PreToolUse` hook the
  *original* `tool_input`, and each hook emitting `updatedInput` competes to
  replace it — only one survives. With two registered hooks the one listed
  first silently wins and the other's work is discarded no matter what any
  switch file says. Collapsing them into one dispatcher is what makes the mode
  actually decide anything. stderr is also kept out of the compressed pipe:
  mixing diagnostics into the payload breaks content detection, and error text
  is exactly what must never be summarized away.

- **`~/.claude/statusline.py`** — reads the same ledger filtered by
  `WHERE session_id = <current session>`, so the figure shown is *this*
  session's savings, not the all-time total (that is one `python -m ltc gain`
  away). ltc's and rtk's numbers are shown **separately, never summed**, so it
  is always clear which tool earned what. It no longer reports a "tokens used"
  figure: the old number summed `cache_read_input_tokens` across every turn,
  counting the same context once per turn, and read 626k for a session whose
  real context was 47k.

Rows logged before `session_id` support was added have `session_id = NULL`
and won't count toward any session's total in the statusline, but still count
toward the global `ltc gain` total.

### Content-type routing

`compress_payload(content, content_type="auto", command=None)` prefers the
**command hint** over content sniffing when one is available, because what
produced the output is far more reliable than what it looks like — an
all-passing pytest run resembles no traceback at all yet still wants the
pytest codec. `ltc/commands.py` maps a shell command to a hint and returns
`None` rather than guessing, since a wrong hint routes output to a codec that
will mangle it while `None` merely costs some compression.

Sniffing is the fallback, in order: `json` → `pytest` → `git` → `listing` →
`log` → `generic` → `identity`. The two command-shaped detectors run first
because they are near-certain when they fire and would otherwise be swallowed
by the broader listing/log tests — but since they route to *lossy* codecs they
only match banners nothing else prints (`test session starts`, `== FAILURES ==`,
`Ran N tests in`, `diff --git`, a `commit <sha>` header). `git status` is
deliberately not sniffable: its text is ordinary enough English to misfire on,
and it nearly always arrives with a command hint anyway.

Note `generic` and `log` are the same codec with one difference:
`log` collapses runs of lines that match *after digit masking* (so timestamped
noise folds together), while `generic` requires exact matches. Digit masking is
what makes the log codec strong on real logs and is precisely what would
destroy a numeric table, so unrecognized content gets the conservative variant.

## Measurement harness

```bash
python -m ltc.harness            # runs against samples/ by default
python -m ltc.harness some/dir   # or any directory of sample payloads
```

Reads the compressor registry dynamically, so it covers any compressor added
later without changes. Reports a per-file table (compressor picked, token
counts, reduction %, round-trip `OK`/`lossy`/`FAIL`) plus an average reduction,
the identity rate and the worst-case file. Exit code is non-zero if any sample
that claims reversibility fails to round-trip.

A sample may name the command that produced it in a sidecar file — e.g.
`samples/git_status.txt.cmd` containing `git status`. Without one the sample
measures content sniffing alone; with one it measures the path the hook
actually takes in production. Sidecars are not themselves treated as samples.

Current numbers against `samples/`:

| file              | compressor | reduction | round-trip |
|-------------------|------------|-----------|------------|
| grep_results.txt  | listing    | 18.9%     | OK         |
| git_status.txt    | git        | 38.4%     | lossy      |
| users.json        | json       | 41.1%     | OK         |
| git_diff.txt      | git        | 47.0%     | lossy      |
| ls_long.txt       | listing    | 56.9%     | lossy      |
| traceback.log     | log        | 57.2%     | OK         |
| pytest_run.txt    | pytest     | 70.8%     | lossy      |

Average 47.2%, identity rate 0%, no round-trip failures. (These moved a point
or two from the figures first published: the compressors are unchanged, but
Stage C recalibrated `estimate_tokens()`, so the *ratios* are now measured
against a better model of what a real tokenizer charges.)

**Treat that 47.2% as an upper bound, not a result.** Every file in `samples/`
was written to exercise a codec, by the same person who wrote the codecs. The
real-output corpus below scores **15%** on the same compressors.

### The real-output corpus: `scripts/capture_corpus.py`

```bash
python scripts/capture_corpus.py            # capture real command output + measure
python scripts/capture_corpus.py --measure  # re-measure what's already captured
```

Runs ordinary commands, keeps their actual stdout in `corpus/`, and
scores the codecs against it. The command list is deliberately *not* chosen for
compressibility — that selection bias is the thing this corpus exists to
escape.

`corpus/` is **gitignored**, for the same reason as the ledger: it is
verbatim output from a real machine (absolute paths, branch names, installed
packages). `samples/` stays synthetic so published numbers stay reproducible by
anyone. Never move a file from `corpus/` into `samples/`.

Current numbers, same codecs, real output:

| sample | codec | orig | saved | reduction |
|--------|-------|------|-------|-----------|
| git_branch | identity | 119 | 0 | 0% |
| git_diff (HEAD~1) | git | 11,754 | 283 | **2%** |
| git_log --stat | git | 3,050 | 1,844 | 60% |
| git ls-files | listing | 375 | 84 | 22% |
| git_status | git | 215 | 96 | 45% |
| grep -rn | listing | 3,732 | 482 | 13% |
| ls -la | listing | 361 | 214 | 59% |
| ls -R | listing | 799 | 0 | **0%** |
| **total** | | **20,405** | **3,003** | **15%** |

The two bolded rows are where the tokens actually are. `git diff` is over half
the corpus by volume and yields 2% — context trimming barely dents a diff that
is mostly changed lines. `ls -R` yields nothing because its
`dir:` / bare-name block format isn't a shape the listing codec parses. Those
two, in that order, are worth more than every other codec improvement combined.

## MCP server

`ltc/mcp_server.py` is a stdio-based MCP server exposing three tools —
`compress(content, content_type)`, `decompress(compressed, metadata)`, and
`stats(content, content_type)` — as thin wrappers around the library. This is
the *explicit-invocation* transport: an agent calls these tools deliberately,
the same as any other tool. It does not intercept or auto-compress anything.

Requires the optional `mcp` dependency: `pip install mcp`. The core `ltc`
package stays stdlib-only; only `mcp_server.py` imports `mcp`.

Register it with Claude Code (stdio):

```bash
claude mcp add ltc -- python -m ltc.mcp_server
```

or add directly to your MCP config:

```json
{
  "mcpServers": {
    "ltc": {
      "command": "python",
      "args": ["-m", "ltc.mcp_server"]
    }
  }
}
```

Verified against the real `mcp` client SDK (`stdio_client`/`ClientSession`):
`list_tools()` returns `compress`, `decompress`, `stats`, and `call_tool()`
round-trips correctly.

### Verified against a local model (Ollama)

`scripts/ollama_demo.py` is a standalone demo (not part of the stdlib-only
`ltc` package — it talks to `localhost:11434` via `urllib`) that compresses a
sample traceback with the `log` compressor and asks a local Ollama model the
same question against the raw and compressed versions. Run with:

```bash
python scripts/ollama_demo.py ornith:latest
```

Last verified run (model `ornith:latest`): the `log` compressor reduced a
sample traceback+noise payload from 850 to 291 estimated tokens
(65.8% reduction), and the model gave an equivalent answer
(`NoResultFound` in `app/routes/orders.py`) from both the raw and the
compressed payload. Independently double-checked outside the demo script:
`decompress(compress(x))` reproduces the sample byte-for-byte, and
recomputing `estimate_tokens()` on the original and compressed text matches
the reported 850 -> 291 / 65.76% exactly (not just what the script printed).

## Privacy: what ltc records, and what must never be committed

ltc makes **no network calls** and has no telemetry — the core package is
stdlib-only by design (see `ltc/__init__.py`). The one thing it does persist is
the savings ledger, and that file is sensitive:

`ltc_stats.db` stores one row per compressed call including the **originating
command, verbatim**. On a shared or employer-owned machine those command strings
are that organization's information — absolute paths, branch names, service
names, anything else that appeared on a command line. The ledger is therefore
**gitignored and must stay that way**; it is local-only state, not part of the
project.

Everything in `samples/` is synthetic by design, so the published measurement
numbers are reproducible by anyone without exposing a real codebase. Keep it
that way: benchmark against `samples/` or public repositories, never against
private output.

## License

Licensed under the Apache License 2.0 — see [LICENSE](LICENSE) and
[NOTICE](NOTICE). Apache-2.0 rather than MIT for its explicit patent grant and
its requirement that downstream modifications be marked.

This is an independent personal project, developed on personally owned
equipment, outside the scope of any employment, and without use of any
employer's confidential information or facilities.
