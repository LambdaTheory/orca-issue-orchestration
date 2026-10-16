#!/usr/bin/env bash
# Durable-signal watcher: polls GitHub for PR head commits, PR/issue comments and new PRs.
# Prints one line per change and exits after the first poll that finds changes (re-arm after handling).
# Snapshot lives in $WATCH_DIR/snapshot.txt. Usage: WATCH_DIR=<repo>/.orchestration/<run> WATCH_REPO=owner/name gh_watch.sh [--resync]
#
# Output lines:
#   changed: <entry>          entry is new, or its snapshot line changed while still open
#   changed: gone: <entry> state=<S>   entry left the open list; gh view reports its real state
#                             (MERGED, CLOSED, ...) so the coordinator can tell them apart
#
# With --resync the exact same full diff is computed (new + changed + departed
# entries, with real states for the departed), and every report line is emitted
# with the single "absorbed" prefix instead of changed:/gone: — resync reconciles
# a possibly stale local snapshot into the current remote state; it is not a
# live-signal poll:
#   absorbed (check it is yours): <entry>[ state=<S>]
#
# Any gh failure (list query or state query) keeps the existing snapshot untouched,
# passes gh's own stderr through plus a clear message, and exits non-zero instead of
# polling forever or pretending to be online. A failing write_snapshot also exits
# non-zero — snapshot persistence is never reported as success. A legitimately empty
# open list is a valid state, including the transition from non-empty to all-empty.
# No snapshot.txt is created when the initial snapshot fails.
set -u
set -o pipefail
cd "${WATCH_DIR:?set WATCH_DIR to the run state dir}" || exit 1
repo=${WATCH_REPO:?set WATCH_REPO=owner/name}
interval=${WATCH_INTERVAL:-60}

# Per-process private temp dir: parallel watchers/resyncs sharing the same
# WATCH_DIR must not race on shared fixed tmp names, so each run stages
# everything inside its own mktemp -d dir and the trap removes only that dir.
# Only one watcher may own a run; stop it before --resync. Keep staging on the
# snapshot filesystem so the final rename does not become a cross-device copy.
tmp_dir=$(mktemp -d "./.gh-watch.XXXXXX") || { echo "gh_watch: mktemp -d failed" >&2; exit 1; }
trap 'rm -rf "$tmp_dir"' EXIT

die() { echo "gh_watch: $*" >&2; exit 1; }

snap_once() {
  # Both queries must succeed; otherwise fail so callers keep the old snapshot.
  # An empty result is a legitimate empty open list, not a failure.
  # gh's stderr is passed through, not swallowed, so the raw reason survives.
  gh pr list -R "$repo" --state open --json number,headRefOid,comments \
    -q '.[]|"pr\(.number) head=\(.headRefOid[0:7]) comments=\(.comments|length)"' > "$tmp_dir/snap.prs.tmp" || return 1
  gh issue list -R "$repo" --state open --json number,comments \
    -q '.[]|"issue\(.number) comments=\(.comments|length)"' > "$tmp_dir/snap.issues.tmp" || return 1
  cat "$tmp_dir/snap.prs.tmp" "$tmp_dir/snap.issues.tmp"
}
snap() { local i; for i in 1 2 3 4 5; do snap_once && return 0; sleep 3; done; return 1; }

write_snapshot() {
  # $1 = sorted snapshot content, possibly empty. Staged via a temp file and mv so
  # a failing run never leaves an empty or partial snapshot.txt behind.
  # Returns non-zero on any failure; callers must die so a failed write is never
  # reported as success.
  if [ -n "$1" ]; then printf '%s\n' "$1" > "$tmp_dir/snapshot.txt.tmp" || return 1; else : > "$tmp_dir/snapshot.txt.tmp" || return 1; fi
  mv -f "$tmp_dir/snapshot.txt.tmp" snapshot.txt
}

compute_diff() {
  # The single full diff used by both the watch loop and --resync: appearing /
  # changed entries plus departed entries with their real state. $1 = old sorted
  # content (possibly empty), $2 = new sorted content, $3 = output prefix for
  # appearing/changed entries, $4 = output prefix for departed entries.
  # Report lines go to stdout (appearing/changed first, then departures).
  # Returns 1 without emitting report lines when reconciliation fails: an
  # unrecognized snapshot line, or gh view failing for a departed entry. The
  # caller then keeps the existing snapshot and exits non-zero.
  local old=$1 now=$2 added_prefix=$3 gone_prefix=$4
  local line kindnum kind num state added
  if [ -n "$old" ]; then printf '%s\n' "$old" | sort > "$tmp_dir/old.tmp"; else : > "$tmp_dir/old.tmp"; fi
  if [ -n "$now" ]; then printf '%s\n' "$now" | sort > "$tmp_dir/now.tmp"; else : > "$tmp_dir/now.tmp"; fi
  : > "$tmp_dir/gone_report.tmp"
  added=$(comm -13 "$tmp_dir/old.tmp" "$tmp_dir/now.tmp")
  comm -23 "$tmp_dir/old.tmp" "$tmp_dir/now.tmp" > "$tmp_dir/gone.tmp"

  # Entries that left the open list: resolve their real state via gh view.
  # If the same entry number is still open with a changed line, that is an
  # update (covered by the appearing prefix), not a departure.
  while IFS= read -r line; do
    [ -n "$line" ] || continue
    case $line in
      pr[0-9]*\ *|issue[0-9]*\ *) ;;
      *) echo "gh_watch: unrecognized snapshot line: $line" >&2; return 1 ;;
    esac
    kindnum=${line%% *}
    if grep -Fq -- "$kindnum " "$tmp_dir/now.tmp"; then continue; fi
    case $kindnum in pr*) kind=pr; num=${kindnum#pr} ;; issue*) kind=issue; num=${kindnum#issue} ;; esac
    if state=$(gh "$kind" view "$num" -R "$repo" --json state -q .state < /dev/null) && [ -n "$state" ]; then
      printf '%s: %s state=%s\n' "$gone_prefix" "$kindnum" "$state" >> "$tmp_dir/gone_report.tmp"
    else
      echo "gh_watch: failed to resolve state of $kindnum (gh $kind view $num); keeping existing snapshot" >&2
      return 1
    fi
  done < "$tmp_dir/gone.tmp"

  if [ -n "$added" ]; then printf '%s\n' "$added" | sed "s/^/$added_prefix: /"; fi
  cat "$tmp_dir/gone_report.tmp"
}

if [ "${1:-}" = "--resync" ]; then
  now=$(snap | sort) || die "gh query failed during resync; keeping existing snapshot"
  if [ -f snapshot.txt ]; then
    old_sorted=$(sort snapshot.txt)
    compute_diff "$old_sorted" "$now" "absorbed (check it is yours)" "absorbed (check it is yours)" \
      || die "resync failed to reconcile the existing snapshot; keeping existing snapshot"
  fi
  write_snapshot "$now" || die "failed to update snapshot.txt; resync result not recorded"
  exit 0
fi

# Initial snapshot: on gh failure, no (empty) snapshot.txt may be created.
if [ ! -f snapshot.txt ]; then
  now=$(snap | sort) || die "gh query failed while building the initial snapshot; no snapshot.txt created"
  write_snapshot "$now" || die "failed to write snapshot.txt; no snapshot recorded"
fi

while true; do
  now=$(snap | sort) || die "gh query failed; keeping existing snapshot"
  old=$(cat snapshot.txt)
  if [ "$now" = "$old" ]; then sleep "$interval"; continue; fi

  if ! compute_diff "$old" "$now" changed "changed: gone" > "$tmp_dir/report.tmp"; then
    # Reason already printed on stderr; keep the snapshot and fail loudly.
    exit 1
  fi

  # Complete, successful reconciliation: only now advance the snapshot, then report.
  write_snapshot "$now" || die "failed to update snapshot.txt; changes were not recorded"
  cat "$tmp_dir/report.tmp"
  exit 0
done