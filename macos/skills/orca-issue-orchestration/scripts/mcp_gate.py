#!/usr/bin/env python3
"""Executable gates for the Wayfinder MCP handoff in this suite.

Two callers use these rules: the orca-issue-orchestration coordinator runs the
CLI before handing a requirement back to the build flow (dispatch precheck in
references/preparation.md, answer intake in references/requirements.md), and
the tests exercise the same functions. Each function is a pure decision over
the current Wayfinder MCP map_read snapshot plus small record dicts. No map
store, sync engine or model scheduling lives here: the MCP service stays the
sole authority, and this script never writes anywhere.
"""
import argparse
import json
import sys

MAP_TOOLS = ("map_create", "map_list", "map_read", "map_apply", "map_wait")
OLD_MAP_ACTIONS = frozenset({
    "migrate_old_map", "write_back_old_map", "comment_old_map",
    "close_old_map", "rewrite_old_map", "dual_write_old_map",
})


def check_tools(tool_list):
    """Missing any of the five Map tools pauses every workflow.

    No bypass is produced: bypass_writes stays empty and the caller must stop.
    """
    tools = tool_list or []
    missing = [t for t in MAP_TOOLS if t not in tools]
    if missing:
        return {"decision": "pause", "paused": True, "bypass_writes": [],
                "reason": "missing Wayfinder MCP tools: " + ", ".join(missing)
                          + "; pause and say so, no HTTP write and no substitute map",
                "forbidden": ["http credential bypass", "github substitute map",
                              "local map store"]}
    return {"decision": "proceed", "paused": False, "bypass_writes": [],
            "reason": "all Wayfinder MCP tools declared; this does not prove a live connection"}


def check_gate(snapshot, prd_synced, scope_confirmed, implement_authorized):
    """Check an unmodified map_read envelope, not a substitute Map format.

    All tickets must be settled; claim is an independent field, not a status.
    Any remaining Fog blocks this conservative mechanical gate; the Agent must
    still check relevance, undecided choices and the PRD's actual contents.
    """
    if not isinstance(snapshot, dict) or snapshot.get("kind") != "found":
        return {"decision": "deny", "reasons": ["map_read.kind must be found"]}
    revision = snapshot.get("revision")
    if not isinstance(revision, dict):
        return {"decision": "deny", "reasons": ["missing map_read revision object"]}
    map_id = revision.get("mapId")
    if not isinstance(map_id, str) or not map_id.strip():
        return {"decision": "deny", "reasons": ["missing or invalid revision.mapId"]}
    number = revision.get("revision")
    if type(number) is not int or number <= 0:
        return {"decision": "deny", "reasons": ["revision.revision must be a positive integer"]}
    state = revision.get("state")
    if not isinstance(state, dict):
        return {"decision": "deny", "reasons": ["missing revision.state"]}
    if state.get("id") != map_id:
        return {"decision": "deny", "reasons": ["revision.state.id differs from revision.mapId"]}
    if type(state.get("currentRevision")) is not int or state["currentRevision"] != number:
        return {"decision": "deny", "reasons": ["revision.state.currentRevision differs from revision.revision"]}
    if not isinstance(state.get("tickets"), list):
        return {"decision": "deny", "reasons": ["missing revision.state.tickets"]}
    if not isinstance(state.get("fog"), list):
        return {"decision": "deny", "reasons": ["missing revision.state.fog"]}
    invalid = []
    for field in ("tickets", "fog", "scopeExclusions"):
        items = state.get(field, [])
        if not isinstance(items, list):
            invalid.append(field + " not a list")
            continue
        invalid.extend(f"{field}[{index}] not an object"
                       for index, item in enumerate(items) if not isinstance(item, dict))
    if invalid:
        return {"decision": "deny", "reasons": invalid}
    tickets = state["tickets"]
    reasons = []
    unfinished = [str(t.get("id", "<missing id>")) for t in tickets
                  if t.get("status") != "settled"]
    if unfinished:
        reasons.append("unfinished or unknown-status map tickets: " + ", ".join(unfinished))
    if state["fog"]:
        reasons.append("unresolved map fog: " + ", ".join(
            str(f.get("text", f.get("id", "<missing fog>"))) for f in state["fog"]))
    if not prd_synced:
        reasons.append("PRD not synced with the settled decisions")
    if not scope_confirmed:
        reasons.append("scope not confirmed")
    if not implement_authorized:
        reasons.append("implementation not authorized")
    if reasons:
        return {"decision": "deny", "reasons": reasons}
    return {"decision": "allow",
            "reasons": ["map tickets settled, no fog, PRD synced, "
                        "scope confirmed, implementation authorized"]}


def handle_answer(answer, processed):
    """Compare native Desk answer keys only with caller-supplied receipts.

    This is not durable exactly-once processing. The same clarification
    checkpoint keeps receipts; service Settlements remain authoritative.
    """
    missing = [field for field in ("ticketId", "answerId", "questionVersion")
               if answer.get(field) is None]
    if missing:
        return {"decision": "reject", "missing_fields": missing,
                "reason": "missing answer fields: " + ", ".join(missing)}
    key = [answer["ticketId"], answer["answerId"], answer["questionVersion"]]
    if key in (processed or []):
        return {"decision": "reject",
                "reason": "answer already processed once: " + str(answer["answerId"]),
                "key": key}
    return {"decision": "accept", "key": key,
            "reason": "answer key absent from supplied processed keys"}


def check_answer_version(answer, current):
    """Match a native Desk AnswerItem to a current map_read Desk projection.

    Revision and PRD version increases alone do not expire an answer. The
    Agent separately verifies Settlement bindings and substantive PRD changes.
    """
    required = ("ticketId", "questionId", "questionVersion", "answerId")
    missing = ["answer." + field for field in required if answer.get(field) is None]
    if current.get("kind") != "found":
        missing.append("current.kind=found")
    revision = current.get("revision")
    if not isinstance(revision, dict):
        missing.append("current.revision")
    elif not isinstance(revision.get("state"), dict):
        missing.append("current.revision.state")
    questions = current.get("deskQuestions")
    if not isinstance(questions, list):
        missing.append("current.deskQuestions")
    if missing:
        return {"decision": "reject", "missing_fields": missing,
                "reason": "missing current answer evidence: " + ", ".join(missing)}
    invalid = [f"deskQuestions[{index}] not an object"
               for index, question in enumerate(questions) if not isinstance(question, dict)]
    if invalid:
        return {"decision": "reject", "reason": "; ".join(invalid)}
    matching = [q for q in questions if q.get("ticketId") == answer["ticketId"]
                and q.get("questionId") == answer["questionId"]]
    question = max(matching, key=lambda q: q.get("questionVersion") or 0, default=None)
    if question is None:
        return {"decision": "reject", "missing_fields": ["current Desk Question"],
                "reason": "no current Desk Question for this answer"}
    question_state = question.get("state")
    if question_state in ("withdrawn", "superseded_by_reopen"):
        return {"decision": "reject", "question_state": question_state,
                "reason": "current Desk Question is " + str(question_state)
                          + "; a withdrawn or superseded question is not a valid answer target"}
    current_answer = question.get("currentAnswer")
    missing = ["current.questionVersion"] if question.get("questionVersion") is None else []
    if not isinstance(current_answer, dict):
        missing.append("current.currentAnswer")
    else:
        missing += ["current.currentAnswer." + field for field in required
                    if current_answer.get(field) is None]
    if missing:
        return {"decision": "reject", "missing_fields": missing,
                "reason": "missing current answer fields: " + ", ".join(missing)}
    stale = [field for field in required if answer[field] != current_answer[field]]
    if answer["questionVersion"] != question["questionVersion"] and "questionVersion" not in stale:
        stale.append("questionVersion")
    if stale:
        return {"decision": "reject", "stale_fields": stale,
                "reason": "stale response, current answer bindings differ: " + ", ".join(stale)}
    if current_answer.get("state") in ("superseded", "cancelled", "closed"):
        return {"decision": "reject", "answer_state": current_answer.get("state"),
                "reason": "current Answer is " + str(current_answer.get("state"))
                          + ", not a live answer"}
    return {"decision": "accept", "reason": "current questionVersion and Answer ID unchanged"}


def check_old_map_actions(actions):
    """Old GitHub issue maps are read-only history: migrating or writing back
    is a failure, and recovery of old answers stays on the old tracker."""
    violations = [a.get("action") for a in (actions or [])
                  if a.get("map") == "legacy_github"
                  and a.get("action") in OLD_MAP_ACTIONS]
    if violations:
        return {"decision": "fail", "violations": violations,
                "reason": "old GitHub map is read-only history: no migration, "
                          "no dual-write, no write-back"}
    return {"decision": "pass",
            "reason": "old GitHub map untouched; history stays on the old tracker"}


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("tools", help="pause when any Map tool is missing")
    p.add_argument("--tools", default="",
                   help="comma-separated tool list the client can provide")

    p = sub.add_parser("gate", help="four-part build handoff gate")
    p.add_argument("--map-snapshot", required=True,
                   help="JSON file holding the current map_read snapshot")
    p.add_argument("--prd-synced", choices=["yes", "no"], required=True)
    p.add_argument("--scope-confirmed", choices=["yes", "no"], required=True)
    p.add_argument("--implement-authorized", choices=["yes", "no"], required=True)

    p = sub.add_parser("answer-once", help="compare a Desk answer with supplied processed keys")
    p.add_argument("--answer", required=True, help="JSON file for the answer")
    p.add_argument("--processed", default="",
                   help="JSON file with already-processed keys; omit for first run")

    p = sub.add_parser("version", help="reject stale answer versions")
    p.add_argument("--answer", required=True, help="JSON file for the answer")
    p.add_argument("--current", required=True,
                   help="JSON file holding the current map_read envelope with deskQuestions")

    p = sub.add_parser("old-map", help="fail any migration or dual-write of the old GitHub map")
    p.add_argument("--actions", required=True, help="JSON file with planned actions")

    args = parser.parse_args(argv)

    def flag(value):
        return value == "yes"

    if args.command == "tools":
        result = check_tools([t.strip() for t in args.tools.split(",") if t.strip()])
    elif args.command == "gate":
        result = check_gate(_read_json(args.map_snapshot), flag(args.prd_synced),
                            flag(args.scope_confirmed), flag(args.implement_authorized))
    elif args.command == "answer-once":
        processed = _read_json(args.processed) if args.processed else []
        result = handle_answer(_read_json(args.answer), processed)
    elif args.command == "version":
        result = check_answer_version(_read_json(args.answer), _read_json(args.current))
    else:
        result = check_old_map_actions(_read_json(args.actions))
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["decision"] in ("allow", "proceed", "accept", "pass") else 3


if __name__ == "__main__":
    sys.exit(main())
