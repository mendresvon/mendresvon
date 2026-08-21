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

The one exception: if a short output is mostly *weight* rather than *content* —
progress-bar redraws and ANSI escapes are more than half its bytes — every line is
still printed, but the cursor animation is dropped. Nothing is removed, only
redrawn frames of the same line.

## Install

```bash
.claude/hooks/install.sh
```

That copies the script to `~/.claude/hooks/` and adds the hook to your user-level
`~/.claude/settings.json`, which applies to **every project and every new session**
on that machine. It is idempotent: re-running updates the existing entry rather than
adding a second one, it merges into an existing `Bash` matcher group instead of
fragmenting your config, it backs up to `settings.json.bak` before writing, and it
refuses to touch a settings file that is not valid JSON. `--uninstall` removes the
entry and leaves your other hooks alone.

Hooks are read at session start, so restart Claude Code (or open `/hooks`) once
after installing.

### Fresh containers and cloud sessions

This repo's own `.claude/settings.json` declares the hook a second time, pointing at
`${CLAUDE_PROJECT_DIR}/.claude/hooks/quiet-output.py`, plus a `SessionStart` hook
that runs `install.sh --quiet`. So a session started in a fresh sandbox — where
`~/.claude` does not exist yet — has filtering active from its first command, and
provisions the user-level copy for the rest of that machine on the way in.

Having both configured is harmless: the classifier refuses to touch a command that
already contains `quiet-output.py`, so a command is wrapped exactly once no matter
how many hooks fire.

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

Progress bars are collapsed (`\r` redraws keep only the final state, and a drawn
bar of box characters becomes `…`), ANSI escapes are stripped, identical lines are
run-length collapsed, and repeats of the same *shape* (same line with different
numbers) are capped at 3 with a count of the rest. Warnings are capped at 10 so a
deprecation flood cannot crowd out a real error. Inside the tail, a run of 5+ lines
sharing a first word (`Compiling …`, `Downloaded …`) collapses to first + count +
last, so the final summary is not pushed out by repetition — error, failure and
summary lines are exempt from that collapse.

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


## Measured on real commands

| Command | Raw | Through the hook | |
| --- | --- | --- | --- |
| `cargo test --release` (rtk's own suite, 2 633 tests, 1 failing) | 2 916 lines / 203 KB | 44 lines / 3.0 KB | 98.5% smaller |
| `cargo build --release` (450 crates) | 285 lines / 8.7 KB | 8 lines / 0.4 KB | 96% smaller |
| `pip download pandas` (over a pty, 31 progress redraws) | 21 lines / 4.4 KB | 21 lines / 1.8 KB | 60% smaller, every line kept |
| `npx vitest run` (6 tests, 2 failing) | 39 lines / 1.4 KB | 39 lines / 1.4 KB | untouched — short |
| `npm install vitest` | 7 lines / 0.2 KB | 7 lines / 0.2 KB | untouched — short |

In the `cargo test` case the surviving 44 lines include the failing test name, the
panicking `file:line`, the assertion message and the full `left:` / `right:` values.
