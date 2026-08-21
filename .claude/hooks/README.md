# quiet-output

A global Claude Code `PreToolUse` hook that keeps noisy command output out of the
context window.

## What it does

When Claude is about to run a Bash command, the hook looks at the command:

* **Noisy command** (install, build, test run, linter, container build, anything
  that prints hundreds of lines and says almost nothing) — the command is
  rewritten to pipe through a filter before its output reaches the model.
* **Anything else** — no decision is returned, the command runs untouched.

The filter writes the **complete** raw output to a log file on disk and hands back
only what matters:

```
[quiet-output] cargo test: 4,213 lines (312.4KB) -> 47 kept, 99% trimmed
[quiet-output] full output: /tmp/claude-quiet-output/20260821-015322-cargo-test-1183.log
--- first 5 lines ---
--- errors / failures / summary lines ---
--- last 25 lines ---
```

Short output is never touched. If the command turns out to have printed less than
80 lines / 8 KB, the filter prints it back **verbatim**, byte for byte. That is
what makes a wrong guess by the classifier harmless.

## Install

```bash
cp quiet-output.py ~/.claude/hooks/quiet-output.py
chmod +x ~/.claude/hooks/quiet-output.py
```

Then in `~/.claude/settings.json`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "~/.claude/hooks/quiet-output.py hook",
            "timeout": 10,
            "statusMessage": "Filtering noisy output…"
          }
        ]
      }
    ]
  }
}
```

Hooks are read at session start, so restart Claude Code (or open `/hooks`) once.

## How the rewrite works

```
cd frontend && npm run build
```

becomes

```
cd frontend && {
npm run build
} 2>&1 | quiet-output.py filter --log /tmp/claude-quiet-output/…-npm-run-build-…log --label 'npm run build'
__quiet_rc=${PIPESTATUS[0]}; ( exit $__quiet_rc )
```

* `${PIPESTATUS[0]}` preserves the original exit status — a failing build still fails.
* A leading `cd … &&` chain is hoisted out of the wrap, so directory changes still
  stick in Claude Code's persistent shell.
* `2>&1` folds stderr in, so a compiler that writes errors to stderr is filtered too.

## What gets caught

Package managers (`npm/pnpm/yarn/bun/pip/uv/poetry/cargo/go/gem/composer/apt/brew`),
build systems (`make/cmake/ninja/gradle/mvn/bazel/dotnet/xcodebuild`), bundlers and
compilers (`tsc/webpack/vite/next/esbuild/gcc/clang/rustc`), test runners
(`pytest/jest/vitest/mocha/rspec/phpunit/go test/cargo test`), linters
(`eslint/pylint/mypy/flake8`), and containers/infra
(`docker build/compose/terraform/ansible/helm`). Plus a generic rule: any unknown
tool invoked with a `build` / `test` / `install` / `ci` / `compile` subcommand.

## What is always left alone

* Anything not on that list — `git`, `ls`, `cat`, `grep`, `find`, `curl`, editors, …
* `--help`, `--version`
* Long-running / streaming commands: `--watch`, `nodemon`, `npm run dev`, `tail -f`
* Interactive commands: `docker run -it`
* Commands already redirected to a file (`> build.log`) or already narrowed by a
  pipe (`npm test | tail -20`) — you asked for that shape on purpose
* Backgrounded tool calls (`run_in_background: true`)
* Heredocs, so nothing quoting-sensitive gets rewrapped

## Kept lines

1. First 5 lines (what the command thinks it is doing)
2. Every error / failure / exception / traceback line, plus up to 3 lines of
   indented context under each (stack frames, `-->` spans, carets)
3. Summary lines anywhere in the output (`N passed, M failed`, `test result:`,
   `BUILD FAILED`, `added 44 packages`, …)
4. Last 25 lines (where the real summary usually lives)

Progress bars are collapsed (`\r` redraws keep only the final state), ANSI escapes
are stripped, identical lines are run-length collapsed, and repeats of the same
*shape* (same line with different numbers) are capped at 3 with a count of the rest.
Warnings are capped at 10 so a deprecation flood cannot crowd out a real error.

## Escape hatches

| Variable | Default | Meaning |
| --- | --- | --- |
| `QUIET_OUTPUT_DISABLE=1` | off | turn the hook off entirely |
| `QUIET_PASSTHROUGH_LINES` | 80 | below this many lines, print verbatim |
| `QUIET_PASSTHROUGH_BYTES` | 8000 | below this many bytes, print verbatim |
| `QUIET_TAIL_LINES` | 25 | how much of the tail to always keep |
| `QUIET_MAX_KEPT` | 60 | cap on flagged lines |
| `QUIET_MAX_WEAK` | 10 | cap on warning-ish lines |
| `QUIET_LOG_DIR` | `$TMPDIR/claude-quiet-output` | where full logs go |
| `QUIET_LOG_KEEP` | 40 | how many logs to retain |

Dry-run the classifier without running anything:

```bash
~/.claude/hooks/quiet-output.py try "npm run build"     # FILTER (npm run)
~/.claude/hooks/quiet-output.py try "git status"        # LEAVE ALONE
```

## Safety

Every failure path in the hook exits 0 with no output, which means "no decision" —
the command runs exactly as Claude wrote it. A bug in this script can never block
or corrupt a tool call. If the filter itself throws, it prints the error and then
the raw output it had buffered.
