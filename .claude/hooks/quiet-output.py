#!/usr/bin/env python3
"""
quiet-output — keep noisy command output out of the context window.

Two modes in one file:

  quiet-output.py hook     PreToolUse hook for the Bash tool. Reads the hook
                           JSON on stdin. If the command looks like an install,
                           a build, a test run or anything else that prints a
                           lot and says little, it rewrites the command to pipe
                           through this same script in filter mode. Everything
                           else is left completely alone.

  quiet-output.py filter   Reads command output on stdin, writes the full raw
                           output to a log file, and prints a digest: the first
                           few lines, every error / failure, and the tail where
                           the summary lives. If the output turned out to be
                           short, it prints it verbatim instead — an untouched
                           passthrough.

  quiet-output.py try CMD  Dry run. Prints whether CMD would be rewritten and
                           what it would be rewritten to.

Design notes:
  * Exit status is preserved (${PIPESTATUS[0]}), so a failing build still fails.
  * A leading `cd ... &&` chain is hoisted out of the wrap so directory changes
    still stick in the persistent shell.
  * The filter is a no-op below the size threshold, which makes a false positive
    in the classifier free: a short command still comes back whole.
  * Any unexpected error in the hook means "no decision" (exit 0, no output), so
    a bug here can never block or mangle a command.
"""

import os
import re
import sys

VERSION = "1.0.0"


# --------------------------------------------------------------------------
# config (all overridable by env var)
# --------------------------------------------------------------------------

def _int(name, default):
    try:
        return max(0, int(os.environ[name]))
    except Exception:
        return default


PASSTHROUGH_LINES = _int("QUIET_PASSTHROUGH_LINES", 80)     # <= this many lines: print verbatim
PASSTHROUGH_BYTES = _int("QUIET_PASSTHROUGH_BYTES", 8000)   # ...and <= this many bytes
HEAD_LINES        = _int("QUIET_HEAD_LINES", 5)
TAIL_LINES        = _int("QUIET_TAIL_LINES", 25)
MAX_KEPT          = _int("QUIET_MAX_KEPT", 60)              # cap on flagged lines
MAX_WEAK          = _int("QUIET_MAX_WEAK", 10)              # cap on warning-ish lines
MAX_PER_SIGNATURE = _int("QUIET_MAX_PER_SIGNATURE", 3)      # cap on repeats of the same shape
MAX_LINE_CHARS    = _int("QUIET_MAX_LINE_CHARS", 400)
CONTEXT_LINES     = _int("QUIET_CONTEXT_LINES", 3)
LOG_KEEP          = _int("QUIET_LOG_KEEP", 40)              # log files to retain

DISABLED = os.environ.get("QUIET_OUTPUT_DISABLE", "").lower() in ("1", "true", "yes")


def log_dir():
    base = os.environ.get("QUIET_LOG_DIR")
    if not base:
        base = os.path.join(os.environ.get("TMPDIR", "/tmp"), "claude-quiet-output")
    return base


# --------------------------------------------------------------------------
# shell-aware parsing helpers
# --------------------------------------------------------------------------

def blank_quoted(cmd):
    """Return cmd with the inside of quoted spans replaced by spaces, so that
    regexes for shell metacharacters do not trip over string literals."""
    out, quote, i = [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            if ch == "\\" and quote == '"' and i + 1 < len(cmd):
                out.append("  ")
                i += 2
                continue
            if ch == quote:
                quote = None
                out.append(ch)
            else:
                out.append(" ")
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < len(cmd):
            out.append("  ")
            i += 2
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def split_segments(cmd):
    """Split a command line on unquoted && || | ; and newlines."""
    segs, buf, quote, i = [], [], None, 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            buf.append(ch)
            if ch == "\\" and quote == '"' and i + 1 < len(cmd):
                buf.append(cmd[i + 1])
                i += 2
                continue
            if ch == quote:
                quote = None
            i += 1
            continue
        if ch in "\"'":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        if ch == "\\" and i + 1 < len(cmd):
            buf.append(ch)
            buf.append(cmd[i + 1])
            i += 2
            continue
        if cmd[i:i + 2] in ("&&", "||"):
            segs.append("".join(buf))
            buf = []
            i += 2
            continue
        if ch in ";|\n&":
            segs.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    segs.append("".join(buf))
    return [s.strip() for s in segs if s.strip()]


def tokenize(segment):
    try:
        import shlex
        return shlex.split(segment, posix=True)
    except Exception:
        return segment.split()


# --------------------------------------------------------------------------
# classifier: which commands are worth filtering
# --------------------------------------------------------------------------

# Tools whose output is noisy no matter the subcommand.
ALWAYS_NOISY = {
    # test runners
    "pytest", "py.test", "jest", "vitest", "mocha", "ava", "karma", "nose2",
    "tox", "nox", "phpunit", "rspec", "cucumber", "nextest", "ctest", "gotestsum",
    # build systems
    "make", "gmake", "ninja", "cmake", "meson", "bazel", "buck", "scons",
    "gradle", "gradlew", "./gradlew", "mvn", "ant", "msbuild", "sbt", "lein",
    "stack", "cabal", "rebar3", "xcodebuild", "waf",
    # bundlers / compilers
    "tsc", "webpack", "rollup", "esbuild", "parcel", "turbo", "nx", "gulp",
    "grunt", "vite", "next", "nuxt", "ng", "snowpack", "browserify",
    "gcc", "g++", "cc", "clang", "clang++", "rustc", "javac", "kotlinc", "scalac",
    # linters / type checkers that stream findings
    "eslint", "stylelint", "pylint", "flake8", "mypy", "pyright", "tflint",
    "shellcheck", "clippy-driver", "golangci-lint",
    # package / system installers
    "apt", "apt-get", "aptitude", "yum", "dnf", "apk", "pacman", "zypper",
    "brew", "port", "choco", "scoop", "gem", "cpanm", "conda", "mamba",
    # infra
    "ansible-playbook", "terraform", "tofu", "pulumi", "vagrant",
    "docker-compose", "podman-compose", "helmfile", "packer",
}

# Tools that are only noisy for particular subcommands.
NOISY_SUBCOMMANDS = {
    "npm":     {"install", "i", "ci", "add", "install-test", "it", "run", "run-script",
                "test", "tst", "t", "build", "update", "up", "audit", "rebuild",
                "pack", "publish", "link", "dedupe", "prune", "exec"},
    "pnpm":    {"install", "i", "add", "run", "test", "build", "update", "rebuild",
                "dlx", "exec", "prune", "fetch", "deploy"},
    "yarn":    {"install", "add", "run", "test", "build", "upgrade", "dlx",
                "workspaces", "why", "dedupe"},
    "bun":     {"install", "i", "add", "run", "test", "build", "update", "x", "pm"},
    "deno":    {"install", "cache", "task", "test", "bundle", "compile", "check"},
    "npx":     set(),          # handled by unwrapping to the real tool
    "pip":     {"install", "download", "wheel", "uninstall", "sync"},
    "pip3":    {"install", "download", "wheel", "uninstall", "sync"},
    "uv":      {"sync", "add", "pip", "install", "build", "run", "venv", "lock", "tool"},
    "uvx":     set(),
    "poetry":  {"install", "add", "update", "build", "lock", "run", "sync"},
    "pipenv":  {"install", "sync", "lock", "update", "run"},
    "pdm":     {"install", "add", "sync", "update", "build", "run"},
    "cargo":   {"build", "b", "test", "t", "check", "c", "clippy", "run", "r",
                "bench", "install", "doc", "update", "fetch", "nextest",
                "tarpaulin", "audit", "publish", "fix", "miri"},
    "rustup":  {"install", "update", "toolchain", "component", "target"},
    "go":      {"build", "test", "get", "install", "mod", "vet", "generate", "run", "work"},
    "dotnet":  {"build", "test", "restore", "publish", "pack", "run", "clean"},
    "swift":   {"build", "test", "package"},
    "docker":  {"build", "buildx", "pull", "push", "compose", "image", "run"},
    "podman":  {"build", "pull", "push", "image", "run"},
    "nerdctl": {"build", "pull", "push"},
    "helm":    {"install", "upgrade", "dependency", "template", "lint"},
    "bundle":  {"install", "update", "exec"},
    "composer": {"install", "update", "require", "dump-autoload"},
    "mix":     {"deps.get", "compile", "test", "do", "release"},
    "flutter": {"build", "test", "pub", "run"},
    "R":       {"CMD"},
    "sam":     {"build", "deploy"},
    "serverless": {"deploy", "package"},
    "cdk":     {"deploy", "synth", "diff"},
}

# Generic fallback: unknown tool, but the subcommand is a classic noisy verb.
GENERIC_VERBS = {"build", "test", "install", "ci", "compile", "bundle",
                 "package", "publish", "deploy", "e2e", "typecheck", "coverage"}

# Never wrapped: coreutils, VCS, editors, and anything already terse.
NEVER = {
    "cat", "bat", "ls", "ll", "echo", "printf", "head", "tail", "grep", "rg",
    "egrep", "fgrep", "sed", "awk", "cut", "tr", "sort", "uniq", "wc", "find",
    "fd", "jq", "yq", "less", "more", "git", "gh", "hg", "svn", "cd", "pwd",
    "cp", "mv", "rm", "mkdir", "rmdir", "touch", "ln", "chmod", "chown", "stat",
    "file", "du", "df", "ps", "top", "htop", "kill", "pkill", "which", "type",
    "whoami", "id", "uname", "hostname", "env", "export", "set", "unset",
    "source", ".", "date", "sleep", "true", "false", "tree", "diff", "patch",
    "tar", "unzip", "zip", "gzip", "gunzip", "ssh", "scp", "rsync", "history",
    "man", "tldr", "open", "code", "vim", "vi", "nano", "emacs", "tmux",
    "screen", "watch", "tee", "read", "test", "[", "seq", "basename", "dirname",
    "realpath", "readlink", "mktemp", "sha256sum", "md5sum", "xxd", "od",
}

# If the last stage of a pipeline is one of these, the caller already narrowed
# the output on purpose. Leave it alone.
PAGERS = {"head", "tail", "grep", "rg", "egrep", "fgrep", "less", "more", "wc",
          "jq", "yq", "sed", "awk", "cut", "sort", "uniq", "column", "fzf",
          "tee", "xargs", "tr", "first", "last"}

SKIP_PATTERNS = [
    re.compile(r"(^|\s)(-h|--help|--version|-V)(\s|$)"),   # -v is verbose, not version
    re.compile(r"--watch|--hot|--reload|(^|\s)-w(\s|$)|nodemon|--serve\b"),
    re.compile(r"(^|\s)(npm|pnpm|yarn|bun|deno)\s+(run\s+)?(dev|start|serve|preview|watch)(\s|$)"),
    re.compile(r"(^|\s)(-it|-ti|--interactive|--tty)(\s|$)"),
    re.compile(r"<<"),                      # heredoc: leave the quoting alone
    re.compile(r"\btail\s+-f\b|\bjournalctl\b.*-f\b"),
]

CD_PREFIX_RE = re.compile(r"^\s*((?:cd|pushd)\s+[^&|;<>\n]+?\s*&&\s*)+")
REDIRECT_RE = re.compile(r"(?<![0-9])>{1,2}\s*[^&\s]")   # > file / >> file, but not 2>&1


def base_tool(token):
    tool = os.path.basename(token)
    if tool.endswith(".exe"):
        tool = tool[:-4]
    return tool


def segment_is_noisy(segment):
    tokens = tokenize(segment)
    # strip leading env assignments and command wrappers
    while tokens:
        t = tokens[0]
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t) or t in (
                "sudo", "command", "time", "nice", "ionice", "nohup", "stdbuf",
                "env", "exec", "timeout", "setsid", "doas"):
            tokens = tokens[1:]
            # `timeout 300 npm test` / `nice -n 5 make`
            while tokens and re.match(r"^-|^\d+[smhd]?$", tokens[0]):
                tokens = tokens[1:]
            continue
        break
    if not tokens:
        return None

    tool = base_tool(tokens[0])
    rest = tokens[1:]

    # unwrap runners that delegate to another tool
    hops = 0
    while tool in ("npx", "uvx", "pnpx", "bunx", "poetry", "pipenv", "uv") and rest and hops < 3:
        if tool in ("poetry", "pipenv", "uv") and rest[0] != "run":
            break
        nxt = [t for t in (rest[1:] if rest[0] == "run" else rest) if not t.startswith("-")]
        if not nxt:
            break
        tool, rest, hops = base_tool(nxt[0]), nxt[1:], hops + 1
    if tool in ("python", "python3", "py") and rest[:1] == ["-m"] and len(rest) > 1:
        tool, rest = base_tool(rest[1]), rest[2:]

    if tool in NEVER:
        return None
    if tool in ALWAYS_NOISY:
        return tool

    args = [t for t in rest if not t.startswith("-")]
    sub = args[0] if args else ""
    subs = NOISY_SUBCOMMANDS.get(tool)
    if subs is not None and sub in subs:
        return "%s %s" % (tool, sub)
    if subs is None and sub in GENERIC_VERBS:
        return "%s %s" % (tool, sub)
    return None


def classify(command):
    """Return (label, cd_prefix, body) if the command should be filtered,
    else None."""
    if DISABLED or not command or len(command) > 8000:
        return None
    if "quiet-output.py" in command or re.search(r"(^|\s)rtk\s", command):
        return None                                   # already filtered

    scan = blank_quoted(command)
    for pat in SKIP_PATTERNS:
        if pat.search(scan):
            return None
    if REDIRECT_RE.search(scan):
        return None                                   # output already goes to a file

    segments = split_segments(command)
    if not segments:
        return None
    last_tokens = tokenize(segments[-1])
    if "|" in scan and last_tokens and base_tool(last_tokens[0]) in PAGERS:
        return None                                   # caller already narrowed it

    labels = [lbl for lbl in (segment_is_noisy(s) for s in segments) if lbl]
    if not labels:
        return None

    m = CD_PREFIX_RE.match(command)
    cd_prefix, body = (m.group(0), command[m.end():]) if m else ("", command)
    if not body.strip():
        return None
    return labels[0], cd_prefix, body


# --------------------------------------------------------------------------
# hook mode
# --------------------------------------------------------------------------

def slugify(label):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-")[:40] or "cmd"


def prepare_log(label):
    import time
    d = log_dir()
    os.makedirs(d, exist_ok=True)
    try:
        files = sorted(
            (os.path.join(d, f) for f in os.listdir(d) if f.endswith(".log")),
            key=os.path.getmtime, reverse=True)
        for old in files[LOG_KEEP:]:
            os.unlink(old)
    except Exception:
        pass
    return os.path.join(d, "%s-%s-%d.log" % (
        time.strftime("%Y%m%d-%H%M%S"), slugify(label), os.getpid()))


def shquote(s):
    import shlex
    return shlex.quote(s)


def run_hook():
    import json
    raw = sys.stdin.read()
    try:
        payload = json.loads(raw) if raw.strip() else {}
    except Exception:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command")
    if not isinstance(command, str) or tool_input.get("run_in_background"):
        return 0

    verdict = classify(command)
    if not verdict:
        return 0
    label, cd_prefix, body = verdict

    me = os.path.realpath(__file__)
    logfile = prepare_log(label)
    filt = "%s %s filter --log %s --label %s" % (
        shquote(sys.executable), shquote(me), shquote(logfile), shquote(label))
    new_command = (
        "%s{\n%s\n} 2>&1 | %s\n__quiet_rc=${PIPESTATUS[0]}; ( exit $__quiet_rc )"
        % (cd_prefix, body.strip(), filt))

    updated = dict(tool_input)
    updated["command"] = new_command
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "updatedInput": updated,
        },
        "systemMessage": (
            "quiet-output: '%s' is filtered — you get errors, failures and the "
            "summary. Full output: %s" % (label, logfile)),
    }))
    return 0


# --------------------------------------------------------------------------
# filter mode
# --------------------------------------------------------------------------

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[()][A-Za-z0-9]|\x1b[=>]")
CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Long runs of box-drawing / block characters are a drawn progress bar.
BAR_RE = re.compile("[\u2500-\u259f]{4,}")

STRONG_RE = re.compile(r"""(?ix)
      \berror(s|ed)?\b | \bERR!\b | \bfail(ed|s|ure|ures|ing)?\b | \bfatal\b
    | \bpanic(ked|:)?\b | \btraceback\b | \bexception\b | \bassert(ion)?\b
    | \bsegmentation\ fault\b | \bcore\ dumped\b | \baborted\b
    | \bcannot\b | \bcan't\b | \bcould\ not\b | \bunable\ to\b
    | \bno\ such\ file\b | \bnot\ found\b | \bpermission\ denied\b
    | \bconnection\ refused\b | \btimed\ out\b | \bunresolved\b
    | \bundefined\ (reference|symbol|is\ not)\b | \bconflict(s|ing)?\b
    | ^\s*[-*]?\s*[✗✖×✘✕]\s
    | ^\s*E\ \ | ^\s*FAIL | ^\s*not\ ok\b | ^\s*\w*Error[:\ ] | ^\s*\w+Exception[:\ ]
    | ^\s*File\ "[^"]+",\ line\ \d+ | ^.{0,120}:\d+:\d+:\s*(error|warning)
""")

WEAK_RE = re.compile(r"(?i)\bwarn(ing|ings)?\b|\bdeprecat(ed|ion)\b|\bskipped\b|\bnotice\b")

SUMMARY_RE = re.compile(r"""(?ix)
      \b\d+\ (passed|failed|skipped|pending|todo|errors?|warnings?|tests?|
              packages?|vulnerabilit\w+|problems?|files?\ changed)\b
    | \btest\ result:\ | ^tests?:\s | ^test\ suites:\ | ^snapshots:\ | ^time:\s
    | \bran\ \d+\ tests?\b | \bbuild\ (succeeded|failed|success|failure)\b
    | \bcompiled\ (successfully|with)\b | \bsuccessfully\ installed\b
    | \b(added|removed|changed|audited)\ \d+\b | \bdone\ in\ | \bfinished\ (in|dev|release)
    | \bbuilt\ in\ | \bexit\ (code|status)\b | \bno\ tests\ found\b
    | ^={3,}.*={3,}$ | ^-{3,}\s*(coverage|summary)
""", re.VERBOSE | re.IGNORECASE)

# Any indented line right after an error is continuation: a stack frame, a
# rustc span, an assertion's left:/right: pair, a pytest E-block.
STACK_RE = re.compile(r"^\s+\S")


def normalize(text):
    if "\r" in text:
        parts = [p for p in text.split("\r") if p.strip()]
        text = parts[-1] if parts else ""
    text = ANSI_RE.sub("", text)
    text = CTRL_RE.sub("", text)
    text = BAR_RE.sub("\u2026", text)
    return text.rstrip()


def signature(text):
    s = re.sub(r"\d+", "#", text.strip())
    s = re.sub(r"\s+", " ", s)
    return s[:120]


def clip(text):
    if len(text) <= MAX_LINE_CHARS:
        return text
    return text[:MAX_LINE_CHARS] + " …(+%d chars)" % (len(text) - MAX_LINE_CHARS)


def collapse_runs(entries, important, min_run=5):
    """Collapse consecutive lines that share a first word into first + count + last.
    Lines flagged as important (errors, failures, summaries) are never collapsed."""
    out, i = [], 0
    while i < len(entries):
        ln, text = entries[i]
        word = text.strip().split(" ")[0] if text.strip() else ""
        j = i
        while (j + 1 < len(entries) and word
               and entries[j + 1][0] not in important
               and entries[j][0] not in important
               and entries[j + 1][1].strip().split(" ")[0:1] == [word]):
            j += 1
        run = j - i + 1
        if run >= min_run:
            out.append(entries[i])
            out.append((None, "… %d more lines starting with '%s'" % (run - 2, word)))
            out.append(entries[j])
        else:
            out.extend(entries[i:j + 1])
        i = j + 1
    return out


class Digest(object):
    def __init__(self, logfile, label):
        self.logfile = logfile
        self.label = label
        self.log = None
        if logfile:
            try:
                os.makedirs(os.path.dirname(logfile), exist_ok=True)
                self.log = open(logfile, "wb")
            except Exception:
                self.log = None
        self.n = 0
        self.bytes = 0              # raw bytes in
        self.norm_bytes = 0         # bytes after collapsing \r redraws and ANSI
        self.raw = []               # verbatim buffer, only while short
        self.norm = []              # same lines, normalized, only while short
        self.overflow = False
        self.head = []
        self.kept = []              # (lineno, text)
        self.strong = 0
        self.weak = 0
        self.flagged = 0
        self.sig_counts = {}
        self.hidden_sig = 0
        self.important = set()      # line numbers never collapsed out of the tail
        self.context_left = 0
        self.last_kept_text = None
        self.dup_run = 0
        from collections import deque
        self.tail = deque(maxlen=TAIL_LINES)

    def feed(self, raw_line):
        self.n += 1
        self.bytes += len(raw_line)
        if self.log:
            try:
                self.log.write(raw_line)
            except Exception:
                self.log = None
        text = normalize(raw_line.decode("utf-8", "replace"))
        self.norm_bytes += len(text) + 1
        if not self.overflow:
            self.raw.append(raw_line)
            self.norm.append(text)
            if self.n > PASSTHROUGH_LINES or self.norm_bytes > PASSTHROUGH_BYTES:
                self.overflow = True
                self.raw = []
                self.norm = []
        if len(self.head) < HEAD_LINES and text.strip():
            self.head.append((self.n, clip(text)))
        if text.strip():
            self.tail.append((self.n, clip(text)))
        self.scan(self.n, text)

    def scan(self, lineno, text):
        if not text.strip():
            self.context_left = 0
            return
        kind = None
        if STRONG_RE.search(text) or SUMMARY_RE.search(text):
            kind = "strong"
        elif WEAK_RE.search(text):
            kind = "weak"
        elif self.context_left > 0 and STACK_RE.match(text):
            kind = "context"

        if not kind:
            self.context_left = max(0, self.context_left - 1)
            return
        if kind != "context":
            self.flagged += 1
        if kind == "strong":
            self.important.add(lineno)
            self.context_left = CONTEXT_LINES
            if self.strong >= MAX_KEPT:
                return
        elif kind == "weak":
            if self.weak >= MAX_WEAK:
                return

        if text == self.last_kept_text:
            self.dup_run += 1
            return
        if self.dup_run:
            self.kept.append((None, "  … previous line repeated %d more times" % self.dup_run))
            self.dup_run = 0
        sig = signature(text)
        seen = self.sig_counts.get(sig, 0) + 1
        self.sig_counts[sig] = seen
        if seen > MAX_PER_SIGNATURE:
            self.hidden_sig += 1
            return
        self.kept.append((lineno, clip(text)))
        self.last_kept_text = text
        if kind == "strong":
            self.strong += 1
        elif kind == "weak":
            self.weak += 1

    def emit(self, out, interrupted=False):
        if self.log:
            try:
                self.log.flush()
                self.log.close()
            except Exception:
                pass
        # short output: hand it back untouched...
        if not self.overflow and not interrupted:
            if self.bytes <= 2 * self.norm_bytes + 512:
                for raw_line in self.raw:
                    out.write(raw_line)
                out.flush()
                return
            # ...unless most of its weight is progress-bar redraw and ANSI.
            # Every line is still printed; only the cursor animation goes.
            note = ("[quiet-output] %s: all %d lines kept, %s of progress-bar"
                    " redraw and ANSI removed\n"
                    % (self.label or "command", self.n,
                       human(self.bytes - self.norm_bytes)))
            out.write(note.encode("utf-8", "replace"))
            out.write(("\n".join(self.norm) + "\n").encode("utf-8", "replace"))
            out.flush()
            return

        if self.dup_run:
            self.kept.append((None, "  … previous line repeated %d more times" % self.dup_run))

        tail = collapse_runs(list(self.tail), self.important)
        tail_start = tail[0][0] if tail else self.n + 1
        kept = [(ln, t) for (ln, t) in self.kept if ln is None or ln < tail_start]
        head = [(ln, t) for (ln, t) in self.head if ln < tail_start
                and not any(k[0] == ln for k in kept)]

        shown = (len(head) + len([k for k in kept if k[0] is not None])
                 + len([t for t in tail if t[0] is not None]))
        pct = int(round(100.0 * (1 - float(shown) / max(1, self.n))))
        w = []
        w.append("[quiet-output] %s%s: %s lines (%s) -> %d kept, %d%% trimmed"
                 % (self.label or "command",
                    " — INTERRUPTED, partial" if interrupted else "",
                    format(self.n, ","), human(self.bytes), shown, pct))
        if self.logfile:
            w.append("[quiet-output] full output: %s" % self.logfile)
        if head:
            w.append("--- first %d lines ---" % len(head))
            w += ["%6d| %s" % (ln, t) for ln, t in head]
        if kept:
            extra = ""
            if self.flagged > self.strong + self.weak:
                extra = " (of %d flagged)" % self.flagged
            w.append("--- errors / failures / summary lines%s ---" % extra)
            for ln, t in kept:
                w.append("%s| %s" % (("%6d" % ln) if ln else "      ", t))
            if self.hidden_sig:
                w.append("      | … %d more repeats of lines already shown" % self.hidden_sig)
        else:
            w.append("--- no error or failure lines matched ---")
        if tail:
            w.append("--- last %d lines ---" % len(self.tail))
            w += ["%s| %s" % (("%6d" % ln) if ln else "      ", t) for ln, t in tail]
        out.write(("\n".join(w) + "\n").encode("utf-8", "replace"))
        out.flush()


def human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.0f%s" % (n, unit) if unit == "B" else "%.1f%s" % (n, unit)
        n /= 1024.0


def run_filter(argv):
    import argparse
    import signal
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument("--log", default="")
    ap.add_argument("--label", default="")
    args, _ = ap.parse_known_args(argv)

    d = Digest(args.log, args.label)
    out = sys.stdout.buffer

    def bail(signum, frame):
        try:
            d.emit(out, interrupted=True)
        except Exception:
            pass
        os._exit(0)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        try:
            signal.signal(sig, bail)
        except Exception:
            pass

    try:
        for raw_line in iter(sys.stdin.buffer.readline, b""):
            d.feed(raw_line)
    except KeyboardInterrupt:
        pass
    except Exception as exc:                     # never swallow the output
        out.write(("[quiet-output] filter error: %r — raw output follows\n"
                   % exc).encode())
        for raw_line in d.raw:
            out.write(raw_line)
        out.flush()
        return 0
    try:
        d.emit(out)
    except BrokenPipeError:
        pass
    return 0


# --------------------------------------------------------------------------

def run_try(argv):
    cmd = " ".join(argv)
    verdict = classify(cmd)
    if not verdict:
        print("LEAVE ALONE: %s" % cmd)
        return 0
    label, cd_prefix, body = verdict
    print("FILTER (%s): %s" % (label, cmd))
    print("  -> %s{ %s } 2>&1 | quiet-output.py filter --label %s"
          % (cd_prefix, body.strip(), label))
    return 0


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "hook"
    if mode == "hook":
        return run_hook()
    if mode == "filter":
        return run_filter(sys.argv[2:])
    if mode == "try":
        return run_try(sys.argv[2:])
    if mode in ("--version", "version"):
        print("quiet-output %s" % VERSION)
        return 0
    sys.stderr.write(__doc__)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # A broken hook must never break the tool call.
        sys.exit(0)
