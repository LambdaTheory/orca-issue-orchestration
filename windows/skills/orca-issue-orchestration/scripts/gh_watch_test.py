#!/usr/bin/env python3
"""Offline coverage for gh_watch.sh: fake gh + fake sleep injected on PATH and
run under the POSIX bash found by find_bash(), so no network is touched.
Covers new/updated/gone entries (MERGED vs CLOSED vs other
states), legitimate empty lists (initial and non-empty -> all-empty), gh failures
(initial, poll, state view) keeping the snapshot, --resync with the full three-gap
diff (absorbed prefix, departed entries with real states, state-query failure
keeping the old snapshot), snapshot-write failure exiting non-zero, per-process
temp staging (no fixed temp files in WATCH_DIR, parallel resync invocations)."""
import os, shutil, stat, subprocess, tempfile, unittest
from pathlib import Path

SCRIPT = Path(__file__).parent / "gh_watch.sh"


def find_git_bash():
    """Locate a real Git Bash bash.exe. WSL (System32\\bash.exe) would get a
    POSIX Windows path, so it is explicitly rejected."""
    candidates = []
    git = shutil.which("git")
    if git:
        for root in list(Path(git).resolve().parents)[:3]:
            candidates.extend((root / "bin/bash.exe", root / "usr/bin/bash.exe"))
    for key in ("ProgramW6432", "ProgramFiles"):
        root = os.environ.get(key)
        if root:
            for sub in ("Git/bin/bash.exe", "Git/usr/bin/bash.exe"):
                candidates.append(Path(root) / sub)
    candidates += [Path(r"C:\Program Files\Git\bin\bash.exe"),
                   Path(r"C:\Program Files (x86)\Git\bin\bash.exe")]
    found = shutil.which("bash")
    if found and not any(part in found.lower() for part in ("system32", "windowsapps")):
        candidates.append(Path(found))
    for cand in candidates:
        if cand.is_file():
            return cand
    raise RuntimeError("Git Bash bash.exe not found (WSL bash is not usable here)")


def find_bash():
    if os.name == "nt":
        return find_git_bash()
    bash = shutil.which("bash")
    if bash:
        return Path(bash)
    raise RuntimeError("bash not found")


BASH = find_bash()

# Test double for gh, configured via marker/answer files in $FAKE_GH_DIR:
#   prs.out / issues.out            -> lines printed for gh pr list / gh issue list
#   prs2.out / issues2.out          -> lines used from the 2nd list call onward
#   fail_pr_list  / fail_issue_list -> list call fails from the start
#   fail_pr_list_2 / fail_issue_list_2 -> list call fails from the 2nd call onward
#   view_pr_<n> / view_issue_<n>    -> state printed by gh pr/issue view <n>
#   fail_view_pr_<n> / fail_view_issue_<n> -> gh view fails for that entry
FAKE_GH = r"""#!/bin/bash
set -u
dir=${FAKE_GH_DIR:?FAKE_GH_DIR must be set}

bump() {
  local f=$1 n=0
  [ -f "$f" ] && n=$(cat "$f")
  n=$((n + 1))
  printf '%s' "$n" > "$f"
}

handle_list() {
  local kind=$1
  local cnt="$dir/${kind}_count" n
  bump "$cnt"
  n=$(cat "$cnt")
  if [ "$n" -ge 2 ]; then
    if [ -f "$dir/fail_${kind}_list_2" ]; then
      echo "fake gh: $kind list failing (later call)" >&2
      exit 3
    fi
    if [ -f "$dir/${kind}s2.out" ]; then
      cat "$dir/${kind}s2.out"
      exit 0
    fi
  fi
  if [ -f "$dir/fail_${kind}_list" ]; then
    echo "fake gh: $kind list failing" >&2
    exit 3
  fi
  if [ -f "$dir/${kind}s.out" ]; then
    cat "$dir/${kind}s.out"
    exit 0
  fi
  echo "fake gh: no $kind list output configured" >&2
  exit 3
}

handle_view() {
  local kind=$1 num=$2
  if [ -f "$dir/fail_view_${kind}_${num}" ]; then
    echo "fake gh: view $kind $num failing" >&2
    exit 3
  fi
  if [ -f "$dir/view_${kind}_${num}" ]; then
    cat "$dir/view_${kind}_${num}"
    exit 0
  fi
  echo "fake gh: no state configured for $kind $num" >&2
  exit 3
}

case " ${1:-} ${2:-} " in
  " pr list ") handle_list pr ;;
  " issue list ") handle_list issue ;;
  " pr view ") handle_view pr "$3" ;;
  " issue view ") handle_view issue "$3" ;;
  *) echo "fake gh: unsupported invocation: $*" >&2; exit 3 ;;
esac
"""

# Watch script sleeps (retry backoff, WATCH_INTERVAL poll pause) are stubbed to
# no-ops so tests stay on the failure/change paths without waiting.
FAKE_SLEEP = "#!/bin/bash\nexit 0\n"


class GhWatch(unittest.TestCase):
    def setUp(self):
        self.top = Path(tempfile.mkdtemp(prefix="中文 gh_watch_"))
        self.watch_dir = self.top / "run"
        self.watch_dir.mkdir()
        self.fake_dir = self.top / "fakebin"
        self.fake_dir.mkdir()
        # Git for Windows bin/bash.exe prepends system tools to PATH. Apply
        # test doubles after that launcher setup, inside the spawned shell.
        self._write(self.top / "bash-env", 'export PATH="$(cd "$GH_WATCH_FAKE_BIN" && pwd):$PATH"\n')
        for name, body in (("gh", FAKE_GH), ("sleep", FAKE_SLEEP)):
            path = self.fake_dir / name
            self._write(path, body)
            path.chmod(path.stat().st_mode | stat.S_IEXEC)
        # Probe: the fake gh must really be executable through PATH, so a
        # PATH or permission problem fails loudly here instead of confusingly later.
        probe_state = self.top / "probe_state"
        probe_state.mkdir()
        self._write(probe_state / "prs.out", "probe-ok\n")
        env = self._env(probe_state)
        probe = subprocess.run([str(BASH), "-c", "gh pr list"], env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, encoding="utf-8", errors="replace",
                               timeout=30)
        self.assertEqual(probe.returncode, 0, probe.stderr)
        self.assertIn("probe-ok", probe.stdout)

    def tearDown(self):
        shutil.rmtree(self.top, ignore_errors=True)

    # -- helpers ------------------------------------------------------------
    def _write(self, path, text):
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)

    def _env(self, fake_state_dir):
        env = os.environ.copy()
        env["WATCH_DIR"] = str(self.watch_dir)
        env["WATCH_REPO"] = "owner/repo"
        env["WATCH_INTERVAL"] = "0"
        env["FAKE_GH_DIR"] = str(fake_state_dir)
        env["GH_WATCH_FAKE_BIN"] = str(self.fake_dir)
        env["BASH_ENV"] = str(self.top / "bash-env")
        env["PATH"] = str(self.fake_dir) + os.pathsep + env.get("PATH", "")
        return env

    def run_watch(self, *args):
        return subprocess.run(
            [str(BASH), str(SCRIPT).replace("\\", "/"), *args],
            cwd=str(self.watch_dir), env=self._env(self.fake_dir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=60)

    def pr_line(self, num, head="deadbee", comments=0):
        return "pr%d head=%s comments=%d" % (num, head, comments)

    def issue_line(self, num, comments=0):
        return "issue%d comments=%d" % (num, comments)

    def set_list(self, kind, first, later=None):
        self._write(self.fake_dir / (kind + "s.out"),
                    "".join(line + "\n" for line in first))
        if later is not None:
            self._write(self.fake_dir / (kind + "s2.out"),
                        "".join(line + "\n" for line in later))

    def fail_list(self, kind, later=False):
        name = "fail_%s_list_2" % kind if later else "fail_%s_list" % kind
        self._write(self.fake_dir / name, "")

    def set_view(self, kind, num, state):
        self._write(self.fake_dir / ("view_%s_%d" % (kind, num)), state + "\n")

    def fail_view(self, kind, num):
        self._write(self.fake_dir / ("fail_view_%s_%d" % (kind, num)), "")

    def write_snapshot(self, lines):
        self._write(self.watch_dir / "snapshot.txt",
                    "".join(line + "\n" for line in lines))

    def read_snapshot(self):
        path = self.watch_dir / "snapshot.txt"
        return path.read_text(encoding="utf-8") if path.exists() else None

    def expected_snapshot(self, lines):
        return "".join(line + "\n" for line in sorted(lines)) if lines else ""

    # -- new entries ----------------------------------------------------------
    def test_new_entry_detected_and_initial_snapshot_created(self):
        pr, issue, new_pr = self.pr_line(11), self.issue_line(5), self.pr_line(12)
        self.set_list("pr", [pr], later=[pr, new_pr])
        self.set_list("issue", [issue], later=[issue])
        proc = self.run_watch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("changed: %s" % new_pr, proc.stdout)
        self.assertNotIn("gone:", proc.stdout)
        self.assertEqual(self.read_snapshot(), self.expected_snapshot([pr, issue, new_pr]))

    def test_updated_entry_reported_changed_not_gone(self):
        old_pr, new_pr = self.pr_line(11, comments=0), self.pr_line(11, comments=2)
        old_is, new_is = self.issue_line(5, comments=0), self.issue_line(5, comments=3)
        self.write_snapshot([old_pr, old_is])
        self.set_list("pr", [new_pr])
        self.set_list("issue", [new_is])
        proc = self.run_watch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("changed: %s" % new_pr, proc.stdout)
        self.assertIn("changed: %s" % new_is, proc.stdout)
        self.assertNotIn("gone: pr11", proc.stdout)
        self.assertNotIn("gone: issue5", proc.stdout)
        self.assertEqual(self.read_snapshot(), self.expected_snapshot([new_pr, new_is]))

    # -- entries leaving the open list ----------------------------------------
    def test_merged_pr_reported_gone_and_snapshot_emptied(self):
        self.write_snapshot([self.pr_line(11)])
        self.set_list("pr", [])
        self.set_list("issue", [])
        self.set_view("pr", 11, "MERGED")
        proc = self.run_watch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("changed: gone: pr11 state=MERGED", proc.stdout)
        self.assertIsNotNone(self.read_snapshot())
        self.assertEqual(self.read_snapshot(), "")

    def test_closed_issue_reported_gone_and_snapshot_emptied(self):
        self.write_snapshot([self.issue_line(5, comments=1)])
        self.set_list("pr", [])
        self.set_list("issue", [])
        self.set_view("issue", 5, "CLOSED")
        proc = self.run_watch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gone: issue5 state=CLOSED", proc.stdout)
        self.assertEqual(self.read_snapshot(), "")

    def test_nonempty_to_empty_transition_reports_each_gone_entry(self):
        self.write_snapshot([self.pr_line(11), self.issue_line(5), self.pr_line(13)])
        self.set_list("pr", [self.pr_line(13)])
        self.set_list("issue", [])
        self.set_view("pr", 11, "MERGED")
        self.set_view("issue", 5, "CLOSED")
        proc = self.run_watch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gone: pr11 state=MERGED", proc.stdout)
        self.assertIn("gone: issue5 state=CLOSED", proc.stdout)
        self.assertEqual(self.read_snapshot(),
                         self.expected_snapshot([self.pr_line(13)]))

    def test_view_reports_state_verbatim(self):
        self.write_snapshot([self.issue_line(7)])
        self.set_list("pr", [])
        self.set_list("issue", [])
        self.set_view("issue", 7, "SOME_FUTURE_STATE")
        proc = self.run_watch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("gone: issue7 state=SOME_FUTURE_STATE", proc.stdout)

    # -- empty list legitimacy ------------------------------------------------
    def test_empty_initial_snapshot_then_new_entry(self):
        self.set_list("pr", [], later=[self.pr_line(11)])
        self.set_list("issue", [], later=[])
        proc = self.run_watch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("gh query failed", proc.stderr)  # empty list != failure
        self.assertIn("changed: %s" % self.pr_line(11), proc.stdout)
        self.assertEqual(self.read_snapshot(), self.expected_snapshot([self.pr_line(11)]))

    # -- gh failures ------------------------------------------------------------
    def test_initial_failure_creates_no_snapshot(self):
        self.set_list("pr", [])
        self.fail_list("pr")
        self.set_list("issue", [])
        proc = self.run_watch()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIsNone(self.read_snapshot())
        self.assertIn("initial snapshot", proc.stderr)

    def test_poll_failure_keeps_snapshot_and_exits_nonzero(self):
        pr, issue = self.pr_line(11), self.issue_line(5)
        self.write_snapshot([pr, issue])
        self.set_list("pr", [pr])
        self.fail_list("issue")
        proc = self.run_watch()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        # Untouched means untouched: the original file content, byte for byte.
        self.assertEqual(self.read_snapshot(), pr + "\n" + issue + "\n")
        self.assertIn("keeping existing snapshot", proc.stderr)

    def test_view_failure_keeps_snapshot_and_exits_nonzero(self):
        gone_pr = self.pr_line(11)
        self.write_snapshot([gone_pr])
        self.set_list("pr", [])
        self.set_list("issue", [])
        self.fail_view("pr", 11)
        proc = self.run_watch()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.read_snapshot(), self.expected_snapshot([gone_pr]))
        self.assertIn("pr11", proc.stderr)

    # -- resync -----------------------------------------------------------------
    def test_resync_reports_absorbed_new_changed_and_gone(self):
        # Full three-gap diff, all under the single absorbed prefix: new entries,
        # an open entry whose line changed, and a departed entry with its real
        # state — resync no longer looks only at additions.
        self.write_snapshot([self.pr_line(11, comments=0), self.pr_line(99)])
        self.set_list("pr", [self.pr_line(11, comments=2), self.pr_line(12)])
        self.set_list("issue", [self.issue_line(5)])
        self.set_view("pr", 99, "MERGED")
        proc = self.run_watch("--resync")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        absorbed = "absorbed (check it is yours): %s"
        self.assertIn(absorbed % self.pr_line(12), proc.stdout)
        self.assertIn(absorbed % self.issue_line(5), proc.stdout)
        self.assertIn(absorbed % "pr99 state=MERGED", proc.stdout)
        self.assertIn(absorbed % self.pr_line(11, comments=2), proc.stdout)
        self.assertNotIn("changed:", proc.stdout)
        self.assertNotIn("gone:", proc.stdout)
        self.assertEqual(self.read_snapshot(),
                         self.expected_snapshot([self.pr_line(11, comments=2),
                                                 self.pr_line(12),
                                                 self.issue_line(5)]))

    def test_resync_empty_list_is_valid(self):
        # Non-empty -> all-empty via resync now reports the departed entry with
        # its real state instead of silently dropping it.
        self.write_snapshot([self.pr_line(11)])
        self.set_list("pr", [])
        self.set_list("issue", [])
        self.set_view("pr", 11, "MERGED")
        proc = self.run_watch("--resync")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("absorbed (check it is yours): pr11 state=MERGED", proc.stdout)
        self.assertEqual(self.read_snapshot(), "")

    def test_resync_gone_view_failure_keeps_old_snapshot_and_exits_nonzero(self):
        # Resync includes the state queries; if one fails, the old snapshot must
        # be byte-identical and the exit non-zero — not a partially reconciled
        # snapshot.
        self.write_snapshot([self.pr_line(11), self.pr_line(99)])
        self.set_list("pr", [self.pr_line(11)])
        self.set_list("issue", [])
        self.fail_view("pr", 99)
        proc = self.run_watch("--resync")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.read_snapshot(),
                         self.expected_snapshot([self.pr_line(11), self.pr_line(99)]))
        self.assertIn("failed to resolve state of pr99", proc.stderr)
        self.assertIn("resync", proc.stderr)

    def test_resync_failure_keeps_snapshot(self):
        self.write_snapshot([self.pr_line(11)])
        self.set_list("pr", [])
        self.fail_list("pr")
        self.set_list("issue", [])
        proc = self.run_watch("--resync")
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertEqual(self.read_snapshot(), self.expected_snapshot([self.pr_line(11)]))
        self.assertIn("resync", proc.stderr)

    # -- snapshot persistence fail-loud & per-process temp dirs ------------------
    def test_write_snapshot_failure_exits_nonzero(self):
        # A failing snapshot write must never look like success: non-zero exit
        # and no snapshot.txt. A failing mv stub isolates just the write path.
        mv = self.fake_dir / "mv"
        self._write(mv, "#!/bin/bash\nexit 1\n")
        mv.chmod(mv.stat().st_mode | stat.S_IEXEC)
        self.set_list("pr", [self.pr_line(11)])
        self.set_list("issue", [])
        proc = self.run_watch()
        self.assertNotEqual(proc.returncode, 0, proc.stdout)
        self.assertIsNone(self.read_snapshot())
        self.assertIn("snapshot.txt", proc.stderr)

    def test_watch_dir_keeps_no_fixed_temp_files(self):
        # Staging happens in the per-process mktemp -d dir, so the shared
        # WATCH_DIR holds only snapshot.txt after a run — parallel processes
        # cannot delete each other's files, and there is nothing to lock.
        # The watcher exits after a found change, so give it one.
        self.set_list("pr", [self.pr_line(11)], later=[self.pr_line(11), self.pr_line(12)])
        self.set_list("issue", [], later=[])
        proc = self.run_watch()
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual([p.name for p in sorted(self.watch_dir.iterdir())],
                         ["snapshot.txt"])

    def test_parallel_resync_processes_use_private_temp_dirs(self):
        # Several resync invocations racing on the same WATCH_DIR must all
        # succeed and leave only snapshot.txt behind.
        self.write_snapshot([self.pr_line(11)])
        self.set_list("pr", [self.pr_line(11)])
        self.set_list("issue", [])
        env = self._env(self.fake_dir)
        procs = [
            subprocess.Popen(
                [str(BASH), str(SCRIPT).replace("\\", "/"), "--resync"],
                cwd=str(self.watch_dir), env=env,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace")
            for _ in range(4)
        ]
        outputs = [p.communicate() for p in procs]
        for proc, (out, err) in zip(procs, outputs):
            self.assertEqual(proc.returncode, 0, err)
            self.assertEqual(out, "")
        self.assertEqual([p.name for p in sorted(self.watch_dir.iterdir())],
                         ["snapshot.txt"])


if __name__ == "__main__":
    unittest.main()