#!/usr/bin/env python3
"""
Part 18 source review as a test (roadmap: "grep the sources for sleep( and usleep( (none in the switch)" and "check that every system and library call result is handled").

  1. NO SLEEPING IN THE SWITCH: sleep, usleep, nanosleep and clock_nanosleep must not be called in any source except host.c (a test client: it paces its own frames) and
     swctl.c (stats --watch sleeps to an absolute deadline, a display refresh).
  2. EVERY RESULT IS HANDLED: a statement that STARTS with a call of a system / pthread / stdio function that can fail, with no `(void)`, no assignment, no `if` and
     no CHECK()/CHECK_PT() around it, is reported. A deliberate "I do not care" is written `(void)call(...)` with a comment.
Comments and string literals are ignored. Diagnostic printing (fprintf / printf / fputs / fwrite to stdout or stderr) is not in the list: nothing can act on its failure.
Exit status 1 if anything is reported.
"""
import os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = os.path.join(ROOT, "src")
SLEEPERS = ("sleep", "usleep", "nanosleep", "clock_nanosleep")
SLEEP_OK = ("host.c", "swctl.c")
CHECKED = r"pthread_\w+|sem_\w+|close|read|write|shutdown|munmap|unlink|fflush|fclose|setsockopt|getsockopt|listen|bind|connect|send|recv|poll|ftruncate|shm_open|shm_unlink|" \
          r"fchmod|chmod|eventfd|accept4|clock_gettime|sigaction|sigwait|sigprocmask|kill|waitpid|fork|execv|dup2|pipe|rename|mkdir|open|socket"
STMT = re.compile(r"^\s*(%s)\s*\(" % CHECKED)
CALL = re.compile(r"\b(%s)\s*\(" % "|".join(SLEEPERS))


def strip(text):
    """Remove comments and string / character literals, keeping the line structure."""
    out, i, n = [], 0, len(text)
    while i < n:
        c = text[i]
        if text.startswith("/*", i):
            j = text.find("*/", i + 2)
            j = n if j < 0 else j + 2
            out.append("".join(ch if ch == "\n" else " " for ch in text[i:j]))
            i = j
        elif text.startswith("//", i):
            j = text.find("\n", i)
            j = n if j < 0 else j
            out.append(" " * (j - i))
            i = j
        elif c in "\"'":
            j = i + 1
            while j < n and text[j] != c:
                j += 2 if text[j] == "\\" else 1
            out.append(c + " " * (j - i - 1) + c)
            i = j + 1
        else:
            out.append(c)
            i += 1
    return "".join(out)


problems = []
files = sorted(f for f in os.listdir(SRC) if f.endswith(".c"))
for f in files:
    code = strip(open(os.path.join(SRC, f)).read()).split("\n")
    for no, line in enumerate(code, 1):
        if f not in SLEEP_OK and CALL.search(line):
            problems.append("%s:%d: sleeping call in the switch: %s" % (f, no, line.strip()))
        if STMT.match(line):
            problems.append("%s:%d: result not handled: %s" % (f, no, line.strip()))
if problems:
    print("static_checks: %d problem(s)" % len(problems))
    for p in problems:
        print("  " + p)
    sys.exit(1)
print("static_checks: %d source files: no sleeping call outside host.c / swctl.c, and no statement ignores the result of a system or pthread call" % len(files))
