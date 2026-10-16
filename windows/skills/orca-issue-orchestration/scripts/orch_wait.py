#!/usr/bin/env python3
"""Hold the one orchestration waiter and swallow liveness heartbeats.

Orca notifies the coordinator for every heartbeat. A check --wait whose
--types omit heartbeat therefore injects "You have 1 orchestration message"
and wakes the model. This process includes heartbeat in --types, so the
waiter receives it. A heartbeat whose subject is "alive" and whose body is
empty is acknowledged here; the process keeps waiting. Any other message,
a failed check or a repeated delivery is printed as the Orca check JSON
and the process exits. Empty timeouts keep waiting without waking the model.

Stdout is that JSON. Stderr is one summary line when any heartbeat was swallowed.
"""
import argparse, json, subprocess, sys

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


def run_loop(check, initial_ack=None):
    """check(ack_id|None) -> envelope dict. Returns (envelope, swallowed, exit_code)."""
    swallowed = 0
    ack = initial_ack
    seen = set()
    while True:
        envelope = check(ack)
        if not isinstance(envelope, dict) or envelope.get("ok") is False:
            return envelope, swallowed, 1
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


def orca_check(orca, run, timeout_ms, terminal, ack):
    cmd = [orca, "orchestration", "check", "--run", run, "--wait",
           "--types", WAKE_TYPES, "--timeout-ms", str(timeout_ms), "--json"]
    if terminal:
        cmd.extend(["--terminal", terminal])
    if ack:
        cmd.extend(["--ack", ack])
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, encoding="utf-8")
    except OSError as exc:
        return {"ok": False, "error": {"code": "check_failed", "message": str(exc)}}
    raw = proc.stdout.strip()
    if proc.returncode != 0 and not raw:
        return {"ok": False, "error": {"code": "check_failed", "message": proc.stderr.strip() or f"exit {proc.returncode}"}}
    try:
        envelope = json.loads(raw)
    except json.JSONDecodeError:
        return {"ok": False, "error": {"code": "bad_json", "message": raw[:500] or proc.stderr.strip()}}
    if proc.returncode != 0 and not (isinstance(envelope, dict) and envelope.get("ok") is False):
        return {"ok": False, "error": {"code": "check_failed",
                "message": proc.stderr.strip() or f"exit {proc.returncode}"}, "response": envelope}
    return envelope


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", required=True)
    p.add_argument("--timeout-ms", type=int, default=1_800_000)
    p.add_argument("--terminal", default="")
    p.add_argument("--ack", default="", help="delivery id already handled; acknowledged on the first wait")
    p.add_argument("--orca", default="orca")
    a = p.parse_args(argv)

    def check(ack):
        return orca_check(a.orca, a.run, a.timeout_ms, a.terminal, ack)

    envelope, swallowed, code = run_loop(check, initial_ack=a.ack or None)
    if swallowed:
        print(f"orch_wait: swallowed {swallowed} liveness heartbeat(s)", file=sys.stderr)
    json.dump(envelope, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    return code


if __name__ == "__main__":
    sys.exit(main())
