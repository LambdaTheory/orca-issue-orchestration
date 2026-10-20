#!/usr/bin/env python3
"""Wait for Orca mail, swallowing empty heartbeats and checking worker health.

Normal mail is returned unchanged. Empty timeouts stay in this process. Read-only
health probes run on a monotonic schedule, not on heartbeat counts. An anomaly
returns a local kind=health_alert, never a synthetic worker_done or delivery.
The coordinator must inspect it before resuming; this script never restarts,
stops or releases a worker. ack_pending names only an unprocessed heartbeat ACK
(or an ACK whose request failed and must be reconciled).

With --guard-dir the same alert envelope also reports guard_unavailable (the
guard terminal's heartbeat went stale) and guard_hold (a prompt is parked for a
human). Both are observations: the coordinator decides, the guard never decides
for it.
"""
import argparse, json, math, subprocess, sys, time
from datetime import datetime
from pathlib import Path

WAKE_TYPES = "worker_done,escalation,question,heartbeat"


def is_liveness(message):
    if not isinstance(message, dict) or message.get("type") != "heartbeat":
        return False
    subject = str(message.get("subject") or "").strip().lower()
    body = str(message.get("body") or "").strip()
    return subject == "alive" and body == ""


def batch_action(result):
    """swallow | timeout | surface. A swallow must have a deliveryId."""
    if not isinstance(result, dict):
        return "surface"
    if result.get("cancelled") or result.get("connectionLost"):
        return "surface"
    messages = result.get("messages") or []
    if not messages:
        return "timeout" if result.get("timedOut") else "surface"
    if all(is_liveness(m) for m in messages) and result.get("deliveryId"):
        return "swallow"
    return "surface"


def with_pending_ack(envelope, ack):
    if ack and isinstance(envelope, dict):
        return {**envelope, "ack_pending": ack}
    return envelope


def run_loop(check, initial_ack=None, health=None, check_now=None):
    """check(ack) -> envelope; health owns its clock and next probe deadline."""
    swallowed = 0
    ack = initial_ack
    seen = set()
    while True:
        if health is not None and health.due():
            alert = health.probe()
            if alert is not None:
                # No second long waiter: drain once after the previous wait ended.
                envelope = check_now(ack)
                if not isinstance(envelope, dict) or envelope.get("ok") is False:
                    return with_pending_ack(envelope, ack), swallowed, 1
                ack = None
                result = envelope.get("result", envelope)
                action = batch_action(result)
                if action == "swallow":
                    delivery = result["deliveryId"]
                    if delivery in seen:
                        return envelope, swallowed, 0
                    swallowed += len(result["messages"])
                    ack = delivery
                elif (not isinstance(result, dict) or result.get("messages")
                      or result.get("deliveryId") or result.get("cancelled")
                      or result.get("connectionLost")):
                    return envelope, swallowed, 0
                # A non-wait check may return a legitimately empty result.
                return with_pending_ack(alert, ack), swallowed, 0
        envelope = check(ack)
        if not isinstance(envelope, dict) or envelope.get("ok") is False:
            return with_pending_ack(envelope, ack), swallowed, 1
        result = envelope.get("result", envelope)
        action = batch_action(result)
        if action == "swallow":
            delivery = result["deliveryId"]
            if delivery in seen:
                return envelope, swallowed, 0
            seen.add(delivery)
            swallowed += len(result["messages"])
            ack = delivery
            continue
        if action == "timeout":
            ack = None
            continue
        return envelope, swallowed, 0


def orca_json(cmd, timeout):
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8", timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "cli_timeout",
                "message": "CLI request timed out; reconcile before retrying any ACK"}}
    except OSError as exc:
        return {"ok": False, "error": {"code": "check_failed", "message": str(exc)}}
    raw = proc.stdout.strip()
    if proc.returncode != 0 and not raw:
        return {"ok": False, "error": {"code": "check_failed", "message": proc.stderr.strip() or f"exit {proc.returncode}"}}
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": {"code": "bad_json", "message": raw[:500] or proc.stderr.strip()}}
    if not isinstance(envelope, dict):
        return {"ok": False, "error": {"code": "bad_response", "message": "Expected an object"}}
    if proc.returncode != 0 and envelope.get("ok") is not False:
        return {"ok": False, "error": {"code": "check_failed",
                "message": proc.stderr.strip() or f"exit {proc.returncode}"}, "response": envelope}
    return envelope


def orca_check(orca, run, timeout_ms, terminal, ack, wait=True):
    cmd = [orca, "orchestration", "check", "--run", run,
           "--types", WAKE_TYPES, "--json"]
    if wait:
        cmd.extend(["--wait", "--timeout-ms", str(timeout_ms)])
    if terminal:
        cmd.extend(["--terminal", terminal])
    if ack:
        cmd.extend(["--ack", ack])
    return orca_json(cmd, timeout_ms / 1000 + 15 if wait else 30)


class ProbeError(Exception):
    def __init__(self, response):
        self.response = response


class WorkerHealth:
    """Run-scoped, in-process observations; liveness is not work progress."""
    def __init__(self, orca, run, interval=60, idle_grace=120, stale_after=900,
                 clock=time.monotonic, request=orca_json):
        self.orca, self.run = orca, run
        self.interval, self.idle_grace, self.stale_after = interval, idle_grace, stale_after
        self.clock, self.request = clock, request
        self.next_probe = clock()
        self.observed = {}

    def due(self):
        return self.clock() >= self.next_probe

    def wait_ms(self, requested):
        return max(1, min(requested, math.ceil((self.next_probe - self.clock()) * 1000)))

    def query(self, args, deadline):
        remaining = deadline - self.clock()
        if remaining <= 0:
            raise ProbeError({"error": {"code": "health_probe_timeout"}})
        response = self.request([self.orca, "orchestration", *args, "--json"], min(30, remaining))
        if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), dict):
            raise ProbeError(response)
        return response["result"]

    def notice(self, reason, dispatch=None, task=None, **evidence):
        commands = [[self.orca, "orchestration", "worker-list", "--run", self.run, "--include-remote", "--json"]]
        if dispatch:
            commands.append([self.orca, "orchestration", "worker-show", "--dispatch", dispatch, "--json"])
        return {"reason": reason, "dispatchId": dispatch, "taskId": task,
                "evidence": evidence, "inspect_commands": commands}

    def probe(self):
        alerts = []
        try:
            deadline = self.clock() + 60
            rows, cursors, cursor = [], set(), None
            for _ in range(20):
                args = ["worker-list", "--run", self.run, "--include-remote"]
                if cursor is not None:
                    args.extend(["--cursor", cursor])
                result = self.query(args, deadline)
                if result.get("scope", {}).get("run", self.run) != self.run or not isinstance(result.get("workers"), list):
                    raise ProbeError({"error": {"code": "invalid_worker_list"}})
                rows.extend(result["workers"])
                page = result.get("page", {})
                if not page.get("hasMore"):
                    break
                cursor = page.get("nextCursor")
                if not isinstance(cursor, str) or not cursor or cursor in cursors:
                    raise ProbeError({"error": {"code": "invalid_health_cursor"}})
                cursors.add(cursor)
            else:
                raise ProbeError({"error": {"code": "health_page_limit"}})

            present = set()
            active = 0
            for row in rows:
                if not isinstance(row, dict) or row.get("runId") != self.run or not row.get("dispatchId"):
                    raise ProbeError({"error": {"code": "invalid_worker_identity"}})
                dispatch, task = row["dispatchId"], row.get("taskId")
                present.add(dispatch)
                settled = row.get("dispatchStatus") in ("completed", "failed", "stopped", "cancelled")
                resource = row.get("terminalState")
                if settled and resource in ("released", "retained"):
                    self.observed.pop(dispatch, None)
                    continue
                active += 1
                detail = self.query(["worker-show", "--dispatch", dispatch], deadline)
                info = detail.get("dispatch", {})
                if info.get("id") != dispatch or info.get("runId") != self.run:
                    raise ProbeError({"error": {"code": "worker_identity_mismatch"}})
                task = info.get("taskId") or info.get("task_id") or task
                projection = detail.get("projection") or row.get("projection") or {}
                stage = projection.get("stage") or {}
                activity = stage.get("activity")
                liveness = (projection.get("liveness") or {}).get("verdict", "unverifiable")
                observation = detail.get("observation") or {}
                agent_wait = observation.get("agentWait")
                facts = {"activity": activity, "liveness": liveness,
                         "dispatch_status": info.get("status"),
                         "agent_wait_known": "agentWait" in observation}
                # Re-check settlement after the list/show race; completion mail wins in run_loop.
                if info.get("status") in ("completed", "failed", "stopped", "cancelled"):
                    release = (detail.get("terminalResource") or {}).get("ownershipState", resource)
                    self.observed.pop(dispatch, None)
                    if release not in ("released", "retained"):
                        alerts.append(self.notice("settlement_pending", dispatch, task, **facts))
                    continue
                if agent_wait:
                    alerts.append(self.notice("needs_input", dispatch, task, **facts))
                    continue
                if liveness != "live":
                    reason = "worker_exited" if liveness == "exited" else "worker_unverifiable"
                    alerts.append(self.notice(reason, dispatch, task, **facts))
                    continue
                now = self.clock()
                output_at = (detail.get("terminal") or {}).get("lastOutputAt")
                signature = (info.get("status"), stage.get("worker"), activity, output_at)
                prior = self.observed.get(dispatch)
                if prior is None or prior["signature"] != signature:
                    prior = {"signature": signature, "since": now, "samples": 0}
                    self.observed[dispatch] = prior
                prior["samples"] += 1
                quiet = now - prior["since"]
                facts.update(quiet_seconds=round(quiet, 1), observations=prior["samples"],
                             last_output_at=output_at)
                if activity in ("done", "idle") and prior["samples"] >= 2 and quiet >= self.idle_grace:
                    alerts.append(self.notice("turn_ended_without_report", dispatch, task, **facts))
                elif quiet >= self.stale_after:
                    alerts.append(self.notice("suspected_stall", dispatch, task, **facts))
            for dispatch in self.observed.keys() - present:
                alerts.append(self.notice("worker_missing", dispatch))
            if not active and not alerts:
                alerts.append(self.notice("no_active_workers"))
        except ProbeError as exc:
            alerts.append(self.notice("health_probe_failed", response=exc.response))
        self.next_probe = self.clock() + self.interval
        if alerts:
            return {"ok": True, "kind": "health_alert", "runId": self.run,
                    "alerts": alerts,
                    "next_action": "Inspect and record a decision before rearming; do not infer death or retry from this alert alone"}
        return None


def positive_number(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number")
    return number


class GuardHealth:
    """Read-only view of the guard terminal state; alerts, never auto-restarts."""
    OPEN_STATES = ("open", "denied-by-agent", "apply-failed")

    def __init__(self, guard_dir, interval=60, stale_after=180, clock=time.monotonic):
        self.dir = Path(guard_dir) if guard_dir else None
        self.interval, self.stale_after = interval, stale_after
        self.clock, self.next_probe = clock, clock()

    def due(self):
        return self.dir is not None and self.clock() >= self.next_probe

    def wait_ms(self, requested):
        return requested

    def _fresh(self, control):
        stamp = control.get("heartbeat_ts")
        if not stamp:
            return False
        try:
            beat = datetime.fromisoformat(stamp)
        except ValueError:
            return False
        return (datetime.now().astimezone() - beat).total_seconds() <= self.stale_after

    def probe(self):
        self.next_probe = self.clock() + self.interval
        if self.dir is None or not self.dir.exists():
            return None
        alerts = []
        try:
            control = json.loads((self.dir / "guard.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            control = {}
        desired = control.get("desired", "unknown")
        if desired not in ("stopped",) and not self._fresh(control):
            alerts.append({"reason": "guard_unavailable", "evidence": {
                "desired": desired, "heartbeat_ts": control.get("heartbeat_ts"),
                "pid": control.get("pid")},
                "inspect_commands": [[str(self.dir / "guard.log")]]})
        holds = []
        for path in sorted((self.dir / "holds").glob("*.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if payload.get("state") in self.OPEN_STATES:
                holds.append({"id": payload.get("id"), "label": payload.get("label"),
                              "tool": payload.get("tool"), "rule": payload.get("rule"),
                              "reason": payload.get("reason"), "state": payload.get("state")})
        if holds:
            alerts.append({"reason": "guard_hold", "evidence": {"holds": holds},
                           "inspect_commands": [[str(self.dir / "audit.jsonl")]]})
        if not alerts:
            return None
        return {"ok": True, "kind": "health_alert", "runId": None, "alerts": alerts,
                "next_action": "确认 hold 的判定依据后再放行或处理；guard_unavailable 先看 guard.log 再用 guard start 幂等恢复"}


class HealthChain:
    """Run several health probes behind the single Health interface run_loop expects."""

    def __init__(self, members):
        self.members = [member for member in members if member is not None]

    def due(self):
        return any(member.due() for member in self.members)

    def wait_ms(self, requested):
        return min([requested, *(member.wait_ms(requested) for member in self.members)])

    def probe(self):
        alerts = []
        for member in self.members:
            if not member.due():
                continue
            envelope = member.probe()
            if envelope:
                alerts.extend(envelope.get("alerts") or [])
        if not alerts:
            return None
        return {"ok": True, "kind": "health_alert", "runId": None, "alerts": alerts,
                "next_action": "Inspect and record a decision before rearming; do not infer death or retry from this alert alone"}


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", required=True)
    p.add_argument("--timeout-ms", type=int, default=1_800_000)
    p.add_argument("--terminal", default="")
    p.add_argument("--ack", default="", help="delivery id already handled; acknowledged on the first wait")
    p.add_argument("--orca", default="orca")
    p.add_argument("--health-interval", type=positive_number, default=60, help="seconds between read-only probes")
    p.add_argument("--idle-grace", type=positive_number, default=120, help="seconds of unchanged done/idle before inspection")
    p.add_argument("--stale-after", type=positive_number, default=900, help="seconds without observable progress before inspection")
    p.add_argument("--guard-dir", default="", help="guard state dir; enables guard health alerts")
    p.add_argument("--guard-stale-after", type=positive_number, default=180, help="seconds without a guard heartbeat before inspection")
    a = p.parse_args(argv)
    if a.timeout_ms <= 0 or a.idle_grace < a.health_interval or a.stale_after < a.idle_grace:
        p.error("require timeout-ms > 0 and health-interval <= idle-grace <= stale-after")
    health = HealthChain([
        WorkerHealth(a.orca, a.run, a.health_interval, a.idle_grace, a.stale_after),
        GuardHealth(a.guard_dir, a.health_interval, a.guard_stale_after) if a.guard_dir else None,
    ])

    def check(ack):
        return orca_check(a.orca, a.run, health.wait_ms(a.timeout_ms), a.terminal, ack)

    def check_now(ack):
        return orca_check(a.orca, a.run, a.timeout_ms, a.terminal, ack, wait=False)

    envelope, swallowed, code = run_loop(check, initial_ack=a.ack or None,
                                         health=health, check_now=check_now)
    if swallowed:
        print(f"orch_wait: swallowed {swallowed} liveness heartbeat(s)", file=sys.stderr)
    json.dump(envelope, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
