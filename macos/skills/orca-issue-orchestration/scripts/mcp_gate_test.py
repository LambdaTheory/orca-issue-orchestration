"""Behaviour-level checks for the Wayfinder MCP handoff gates.

Not grep-the-doc: these call the executable decision functions and the CLI of
mcp_gate.py, including a failure case per boundary, and verify that windows/
and macos/ ship byte-identical content for every suite file this change
touches.
"""
import copy
import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


SKILLS = Path(__file__).resolve().parents[2]
SUITE = SKILLS.parents[1]
SCRIPT_DIR = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "mcp_gate", SCRIPT_DIR / "mcp_gate.py")


def load_gate():
    module = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(module)
    return module


def run_cli(platform_dir, *args):
    result = subprocess.run(
        [sys.executable, "-X", "utf8", str(platform_dir / "mcp_gate.py")] + list(args),
        capture_output=True, text=True)
    return result


class MandatoryMapEntry(unittest.TestCase):
    def test_removed_small_task_command_is_an_argument_error(self):
        cli = run_cli(SCRIPT_DIR, "small-task", "--route", "one_turn")
        self.assertEqual(cli.returncode, 2)
        self.assertIn("invalid choice", cli.stderr)
        self.assertFalse(hasattr(load_gate(), "decide_small_task"))

    def test_removed_map_required_option_is_an_argument_error(self):
        cli = run_cli(SCRIPT_DIR, "tools", "--map-required", "no")
        self.assertEqual(cli.returncode, 2)
        self.assertIn("unrecognized arguments", cli.stderr)


class ToolGate(unittest.TestCase):
    def test_missing_map_tool_pauses_and_produces_no_bypass(self):
        gate = load_gate()
        result = gate.check_tools( ["map_read", "map_list"])
        self.assertEqual(result["decision"], "pause")
        self.assertIn("map_create", result["reason"])
        self.assertIn("map_apply", result["reason"])
        self.assertEqual(result["bypass_writes"], [])
        self.assertIn("http credential bypass", result["forbidden"])
        self.assertIn("local map store", result["forbidden"])

    def test_no_tools_pauses_with_single_argument(self):
        gate = load_gate()
        result = gate.check_tools([])
        self.assertEqual(result["decision"], "pause")
        self.assertEqual(result["bypass_writes"], [])
        for name in gate.MAP_TOOLS:
            self.assertIn(name, result["reason"])
        cli = run_cli(SCRIPT_DIR, "tools")
        self.assertEqual(cli.returncode, 3)
        self.assertEqual(json.loads(cli.stdout), result)
        with self.assertRaises(TypeError):
            gate.check_tools(False, [])

    def test_each_missing_tool_pauses(self):
        gate = load_gate()
        for missing in gate.MAP_TOOLS:
            with self.subTest(tool=missing):
                result = gate.check_tools([t for t in gate.MAP_TOOLS if t != missing])
                self.assertEqual(result["decision"], "pause")
                self.assertIn(missing, result["reason"])

    def test_declared_tool_set_proceeds(self):
        gate = load_gate()
        result = gate.check_tools( list(gate.MAP_TOOLS))
        self.assertEqual(result["decision"], "proceed")


def map_snapshot():
    """Native found/Revision/StoredMapState fixture, matching the MCP envelope."""
    return {"kind": "found", "revision": {
        "mapId": "M1", "revision": 7, "priorRevision": 6, "kind": "apply",
        "author": {"actorId": "human", "clientId": "test", "occurredAt": "2026-11-03T00:00:00Z"},
        "changes": [{"commandIndex": 0, "command": "ticket.settle", "subjectId": "T1"}],
        "state": {"id": "M1", "title": "Requirement", "destination": "Confirmed plan",
                  "notes": "", "fog": [], "scopeExclusions": [], "extensions": {},
                  "currentRevision": 7, "tickets": [{
                      "id": "T1", "title": "Decision", "question": "Which option?",
                      "type": "grilling", "prerequisites": [], "extensions": {},
                      "status": "settled", "claim": None, "settlement": {
                          "outcome": {"kind": "decision", "statement": "A", "rationale": "Human verdict"},
                          "evidence": [], "references": [],
                          "provenance": {"method": "human verdict", "sources": []},
                          "extensions": {}, "introducedAtRevision": 7,
                          "deskAnswer": {"answerId": "a1", "questionVersion": 1}}}]}},
        "frontier": []}


def desk_snapshot():
    snapshot = map_snapshot()
    answer = {"kind": "answer", "ticketId": "T1", "questionId": "q1",
              "answerId": "a1", "questionVersion": 1,
              "choice": {"option": 1, "label": "A"}, "answeredAt": "2026-11-03T00:00:00Z"}
    ticket = snapshot["revision"]["state"]["tickets"][0]
    ticket["status"] = "open"
    ticket["claim"] = "test-session"
    del ticket["settlement"]
    snapshot["deskQuestions"] = [{
        "ticketId": "T1", "questionId": "q1", "questionVersion": 1,
        "question": "Which option?", "options": [{"label": "A", "description": "Use A"},
                                                   {"label": "B", "description": "Use B"}],
        "recommendedOption": 1, "recommendedAnswer": "Use A", "whyKun": "Product choice",
        "triage": "needsKun", "triageReason": "Needs a verdict", "gatesResearch": False,
        "postedAt": "2026-11-03T00:00:00Z", "state": "taken", "currentAnswer": answer,
        "answerHistory": [dict(answer, state="taken", stale=False)],
        "immediateUnlocks": 0, "downstreamUnsettled": 0}]
    return snapshot


class HandoffGate(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate()
        self.full_snapshot = map_snapshot()

    def test_each_missing_part_has_a_named_reason(self):
        for kwargs, needle in [
                ({"prd_synced": False}, "PRD not synced"),
                ({"scope_confirmed": False}, "scope not confirmed"),
                ({"implement_authorized": False}, "implementation not authorized")]:
            args = {"prd_synced": True, "scope_confirmed": True, "implement_authorized": True}
            args.update(kwargs)
            result = self.gate.check_gate(self.full_snapshot, **args)
            self.assertEqual(result["decision"], "deny")
            self.assertIn(needle, " ".join(result["reasons"]))

    def test_found_with_open_claim_and_fog_denies(self):
        snapshot = copy.deepcopy(self.full_snapshot)
        snapshot["revision"]["state"]["tickets"].append({
            "id": "T2", "title": "Next choice", "question": "How?", "type": "grilling",
            "prerequisites": [], "extensions": {}, "status": "open", "claim": "other-session"})
        snapshot["revision"]["state"]["fog"] = [{"id": "F1", "text": "pricing model", "references": []}]
        result = self.gate.check_gate(snapshot, True, True, True)
        self.assertEqual(result["decision"], "deny")
        self.assertIn("T2", " ".join(result["reasons"]))
        self.assertIn("pricing model", " ".join(result["reasons"]))

    def test_unknown_missing_or_claimed_status_denies(self):
        for status in ("unknown", "missing", "claimed", None):
            with self.subTest(status=status):
                snapshot = copy.deepcopy(self.full_snapshot)
                ticket = snapshot["revision"]["state"]["tickets"][0]
                if status is None:
                    del ticket["status"]
                else:
                    ticket["status"] = status
                result = self.gate.check_gate(snapshot, True, True, True)
                self.assertEqual(result["decision"], "deny")
                self.assertIn("T1", " ".join(result["reasons"]))

    def test_non_found_or_missing_native_structure_denies(self):
        for snapshot in ({}, {"kind": "error"}, {"kind": "found"},
                         {"kind": "found", "revision": 7},
                         {"kind": "found", "revision": {}},
                         {"kind": "found", "revision": {"state": {}}}):
            with self.subTest(snapshot=snapshot):
                self.assertEqual(self.gate.check_gate(snapshot, True, True, True)["decision"], "deny")
        snapshot = map_snapshot()
        del snapshot["revision"]["state"]["fog"]
        self.assertEqual(self.gate.check_gate(snapshot, True, True, True)["decision"], "deny")

    def test_fog_denies_even_when_all_tickets_settled(self):
        snapshot = map_snapshot()
        snapshot["revision"]["state"]["fog"] = [{"id": "F1", "text": "pricing model", "references": []}]
        result = self.gate.check_gate(snapshot, True, True, True)
        self.assertEqual(result["decision"], "deny")
        self.assertIn("pricing model", " ".join(result["reasons"]))

    def test_valid_empty_map_allows_without_invented_tickets(self):
        snapshot = map_snapshot()
        snapshot["revision"]["state"]["tickets"] = []
        self.assertEqual(self.gate.check_gate(snapshot, True, True, True)["decision"], "allow")
        for flags in ((False, True, True), (True, False, True), (True, True, False)):
            self.assertEqual(self.gate.check_gate(snapshot, *flags)["decision"], "deny")
        snapshot["revision"]["state"]["fog"] = [{"id": "F1", "text": "Unclear scope"}]
        self.assertEqual(self.gate.check_gate(snapshot, True, True, True)["decision"], "deny")

    def test_missing_map_identity_revision_or_state_denies(self):
        for path in (("revision",), ("revision", "mapId"), ("revision", "revision"),
                     ("revision", "state"), ("revision", "state", "id"),
                     ("revision", "state", "currentRevision")):
            with self.subTest(path=path):
                snapshot = map_snapshot()
                parent = snapshot
                for key in path[:-1]:
                    parent = parent[key]
                del parent[path[-1]]
                self.assertEqual(self.gate.check_gate(snapshot, True, True, True)["decision"], "deny")

    def test_invalid_identity_or_revision_denies(self):
        cases = [("mapId", value) for value in (None, "", "  ", 7, False)]
        cases += [("revision", value) for value in (None, 0, -1, True, 7.0, "7")]
        for field, value in cases:
            with self.subTest(field=field, value=value):
                snapshot = map_snapshot()
                snapshot["revision"][field] = value
                self.assertEqual(self.gate.check_gate(snapshot, True, True, True)["decision"], "deny")

    def test_mismatched_state_identity_or_revision_denies(self):
        for field, value in (("id", "M2"), ("currentRevision", 8),
                             ("currentRevision", "7"), ("currentRevision", 7.0),
                             ("currentRevision", True)):
            with self.subTest(field=field, value=value):
                snapshot = map_snapshot()
                snapshot["revision"]["state"][field] = value
                result = self.gate.check_gate(snapshot, True, True, True)
                self.assertEqual(result["decision"], "deny")
                self.assertIn(field, " ".join(result["reasons"]))

    def test_open_ticket_denies_even_without_fog(self):
        snapshot = map_snapshot()
        snapshot["revision"]["state"]["tickets"][0]["status"] = "open"
        self.assertEqual(self.gate.check_gate(snapshot, True, True, True)["decision"], "deny")

    def test_non_object_map_elements_deny_without_traceback(self):
        with tempfile.TemporaryDirectory() as name:
            snapshot_path = Path(name) / "snapshot.json"
            for field in ("tickets", "fog", "scopeExclusions"):
                for value in ("not-an-object", 42, None, [], False):
                    with self.subTest(field=field, value=value):
                        snapshot = map_snapshot()
                        items = snapshot["revision"]["state"][field]
                        index = len(items)
                        items.append(value)
                        reason = f"{field}[{index}] not an object"
                        result = self.gate.check_gate(snapshot, True, True, True)
                        self.assertEqual(result["decision"], "deny")
                        self.assertIn(reason, result["reasons"])
                        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
                        cli = run_cli(SCRIPT_DIR, "gate", "--map-snapshot", str(snapshot_path),
                                      "--prd-synced", "yes", "--scope-confirmed", "yes",
                                      "--implement-authorized", "yes")
                        self.assertEqual(cli.returncode, 3)
                        self.assertEqual(json.loads(cli.stdout), result)
                        self.assertEqual(cli.stderr, "")

    def test_all_four_parts_present_allow(self):
        self.assertEqual(self.gate.check_gate(self.full_snapshot, True, True, True)["decision"], "allow")


class DeskAnswerGate(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate()
        self.answer = {"ticketId": "T1", "answerId": "a1",
                       "questionVersion": 1}
        self.processed = [["T1", "a1", 1]]

    def test_first_processing_accepts_second_rejects(self):
        self.assertEqual(self.gate.handle_answer(self.answer, [])["decision"],
                         "accept")
        second = self.gate.handle_answer(self.answer, self.processed)
        self.assertEqual(second["decision"], "reject")
        self.assertIn("already processed", second["reason"])

    def test_cli_rejects_duplicate_answer_with_exit_3(self):
        duplicate = {"ticketId": "T1", "answerId": "a1", "questionVersion": 1}
        with tempfile.TemporaryDirectory() as name:
            answer_path = Path(name) / "answer.json"
            processed_path = Path(name) / "processed.json"
            answer_path.write_text(json.dumps(duplicate), encoding="utf-8")
            processed_path.write_text(json.dumps(self.processed), encoding="utf-8")
            first = run_cli(SCRIPT_DIR, "answer-once", "--answer", str(answer_path))
            self.assertEqual((first.returncode, json.loads(first.stdout)["decision"]),
                             (0, "accept"))
            second = run_cli(SCRIPT_DIR, "answer-once", "--answer", str(answer_path),
                             "--processed", str(processed_path))
            self.assertEqual((second.returncode, json.loads(second.stdout)["decision"]),
                             (3, "reject"))


class VersionGate(unittest.TestCase):
    def setUp(self):
        self.gate = load_gate()
        self.current = desk_snapshot()
        self.answer = copy.deepcopy(self.current["deskQuestions"][0]["currentAnswer"])

    def test_only_revision_growth_does_not_expire_answer(self):
        self.current["revision"]["revision"] = 9
        self.current["revision"]["state"]["currentRevision"] = 9
        self.current["revision"]["changes"] = [{"commandIndex": 0, "command": "claim.acquire", "subjectId": "T2"}]
        self.assertEqual(self.gate.check_answer_version(self.answer, self.current)["decision"], "accept")

    def test_changed_question_or_answer_is_rejected(self):
        for field, value in (("questionVersion", 2), ("answerId", "a2")):
            with self.subTest(field=field):
                current = copy.deepcopy(self.current)
                current["deskQuestions"][0]["currentAnswer"][field] = value
                result = self.gate.check_answer_version(self.answer, current)
                self.assertEqual(result["decision"], "reject")
                self.assertIn(field, result["stale_fields"])

    def test_missing_required_fields_are_named_and_rejected(self):
        result = self.gate.check_answer_version({}, {})
        self.assertEqual(result["decision"], "reject")
        self.assertIn("answer.answerId", result["missing_fields"])
        for field in ("ticketId", "questionId", "questionVersion", "answerId"):
            answer = dict(self.answer)
            del answer[field]
            self.assertIn("answer." + field, self.gate.check_answer_version(answer, self.current)["missing_fields"])
            current = copy.deepcopy(self.current)
            del current["deskQuestions"][0]["currentAnswer"][field]
            result = self.gate.check_answer_version(self.answer, current)
            self.assertEqual(result["decision"], "reject")
            self.assertIn("current.currentAnswer." + field, result["missing_fields"])
        current = copy.deepcopy(self.current)
        del current["deskQuestions"][0]["questionVersion"]
        self.assertIn("current.questionVersion", self.gate.check_answer_version(self.answer, current)["missing_fields"])

    def test_older_publication_does_not_hide_current_question_version(self):
        older = copy.deepcopy(self.current["deskQuestions"][0])
        older["state"] = "withdrawn"
        older["currentAnswer"] = None
        current = copy.deepcopy(self.current)
        current["deskQuestions"][0]["questionVersion"] = 2
        current["deskQuestions"][0]["currentAnswer"]["questionVersion"] = 2
        current["deskQuestions"].insert(0, older)
        answer = dict(self.answer, questionVersion=2)
        self.assertEqual(self.gate.check_answer_version(answer, current)["decision"], "accept")
        self.assertEqual(self.gate.check_answer_version(self.answer, current)["decision"], "reject")

    def test_withdrawn_or_superseded_current_question_is_rejected(self):
        for state in ("withdrawn", "superseded_by_reopen"):
            current = copy.deepcopy(self.current)
            current["deskQuestions"][0]["state"] = state
            result = self.gate.check_answer_version(self.answer, current)
            self.assertEqual(result["decision"], "reject")
            self.assertEqual(result.get("question_state"), state)

    def test_version_requires_a_real_found_revision_envelope(self):
        for field in ("kind", "revision"):
            current = copy.deepcopy(self.current)
            del current[field]
            self.assertEqual(self.gate.check_answer_version(self.answer, current)["decision"], "reject")
        current = copy.deepcopy(self.current)
        del current["revision"]["state"]
        self.assertEqual(self.gate.check_answer_version(self.answer, current)["decision"], "reject")

    def test_no_current_answer_or_question_is_rejected(self):
        for questions in ([], [dict(self.current["deskQuestions"][0], currentAnswer=None)]):
            current = dict(self.current, deskQuestions=questions)
            self.assertEqual(self.gate.check_answer_version(self.answer, current)["decision"], "reject")

    def test_non_object_desk_questions_reject_without_traceback(self):
        with tempfile.TemporaryDirectory() as name:
            current_path = Path(name) / "current.json"
            answer_path = Path(name) / "answer.json"
            answer_path.write_text(json.dumps(self.answer), encoding="utf-8")
            for value in ("not-an-object", 42, None, [], False):
                with self.subTest(value=value):
                    current = copy.deepcopy(self.current)
                    current["deskQuestions"].append(value)
                    result = self.gate.check_answer_version(self.answer, current)
                    self.assertEqual(result["decision"], "reject")
                    self.assertIn("deskQuestions[1] not an object", result["reason"])
                    current_path.write_text(json.dumps(current), encoding="utf-8")
                    cli = run_cli(SCRIPT_DIR, "version", "--answer", str(answer_path),
                                  "--current", str(current_path))
                    self.assertEqual(cli.returncode, 3)
                    self.assertEqual(json.loads(cli.stdout), result)
                    self.assertEqual(cli.stderr, "")

    def test_non_object_current_answers_reject_without_traceback(self):
        with tempfile.TemporaryDirectory() as name:
            current_path = Path(name) / "current.json"
            answer_path = Path(name) / "answer.json"
            answer_path.write_text(json.dumps(self.answer), encoding="utf-8")
            for value in ("not-an-object", 42, None, [], False):
                with self.subTest(value=value):
                    current = copy.deepcopy(self.current)
                    current["deskQuestions"][0]["currentAnswer"] = value
                    result = self.gate.check_answer_version(self.answer, current)
                    self.assertEqual(result["decision"], "reject")
                    self.assertIn("current.currentAnswer", result["reason"])
                    current_path.write_text(json.dumps(current), encoding="utf-8")
                    cli = run_cli(SCRIPT_DIR, "version", "--answer", str(answer_path),
                                  "--current", str(current_path))
                    self.assertEqual(cli.returncode, 3)
                    self.assertEqual(json.loads(cli.stdout), result)
                    self.assertEqual(cli.stderr, "")

    def test_matching_current_answer_is_accepted(self):
        self.assertEqual(self.gate.check_answer_version(self.answer, self.current)["decision"], "accept")


class OldMapGate(unittest.TestCase):
    def test_migration_or_dual_write_fails(self):
        gate = load_gate()
        for action in gate.OLD_MAP_ACTIONS:
            result = gate.check_old_map_actions(
                [{"map": "legacy_github", "action": action}])
            self.assertEqual(result["decision"], "fail")
            self.assertEqual(result["violations"], [action])
            self.assertIn("no migration", result["reason"])

    def test_read_only_history_reference_passes(self):
        gate = load_gate()
        result = gate.check_old_map_actions(
            [{"map": "legacy_github", "action": "read_history"},
             {"map": "mcp", "action": "map_create"}])
        self.assertEqual(result["decision"], "pass")


class CliWireFormat(unittest.TestCase):
    def test_gate_cli_json_and_exit_codes(self):
        with tempfile.TemporaryDirectory() as name:
            allowed = Path(name) / "allowed.json"
            denied = Path(name) / "denied.json"
            allowed.write_text(json.dumps(
                map_snapshot()), encoding="utf-8")
            denied.write_text(json.dumps({"kind": "error"}),
                              encoding="utf-8")
            ok = run_cli(SCRIPT_DIR, "gate", "--map-snapshot", str(allowed),
                         "--prd-synced", "yes", "--scope-confirmed", "yes",
                         "--implement-authorized", "yes")
            self.assertEqual(ok.returncode, 0)
            self.assertEqual(json.loads(ok.stdout)["decision"], "allow")
            bad = run_cli(SCRIPT_DIR, "gate", "--map-snapshot", str(denied),
                          "--prd-synced", "yes", "--scope-confirmed", "yes",
                          "--implement-authorized", "yes")
            self.assertEqual(bad.returncode, 3)
            self.assertEqual(json.loads(bad.stdout)["decision"], "deny")


class AgentContractText(unittest.TestCase):
    """Static Agent-rule checks only; no live service or model journey is implied."""
    def test_prior_confirmation_never_skips_map_or_reasks_or_invents_tickets(self):
        for relative in ("orca-issue-orchestration/SKILL.md",
                         "orca-issue-orchestration/references/preparation.md",
                         "orca-issue-orchestration/references/requirements.md"):
            text = (SKILLS / relative).read_text(encoding="utf-8")
            for required in ("启动或恢复", "必须调用同套件", "创建或恢复 MCP Map",
                             "不重复问", "不虚构票", "零票", "不跳过 Map"):
                self.assertIn(required, text, relative)
            for old in ("若不需要 Map", "小任务无需 Map", "小改动不强制完整 PRD、Map"):
                self.assertNotIn(old, text, relative)

    def test_readmes_share_the_mandatory_map_contract(self):
        documents = [relative for relative in PlatformParity.SHARED if relative.endswith("README.md")]
        self.assertEqual(documents, ["README.md", "windows/README.md", "macos/README.md"])
        contracts = []
        for relative in documents:
            text = (SUITE / relative).read_text(encoding="utf-8")
            contract = text.split("## 工作流 Map 门禁\n\n", 1)[1].split("\n## ", 1)[0].rstrip()
            for required in ("启动或恢复", "创建或恢复 MCP Map", "零票", "不重复问", "不虚构票",
                             "revision.mapId", "state.currentRevision", "实施授权"):
                self.assertIn(required, contract, relative)
            contracts.append(contract.encode("utf-8"))
        self.assertEqual(contracts[0], contracts[1])
        self.assertEqual(contracts[0], contracts[2])

    def test_explicit_conversation_verdict_uses_one_same_batch_intake(self):
        text = (SKILLS / "wayfinder/SKILL.md").read_text(encoding="utf-8")
        for required in ("explicit conversation verdict on a specific displayed answer",
                         "Pending or Answered Desk Question", "answered in conversation",
                         "settle without ", "in the same ordered ",
                         "Never keep both paths active", "blanket assent to unseen recommendations"):
            self.assertIn(required, text)
        self.assertNotIn("settle a grilling Ticket only with the answer ID", text)

    def test_legacy_trackers_are_read_only_and_never_initialize_new_maps(self):
        for tracker in ("github", "gitlab", "local"):
            text = (SKILLS / "setup-matt-pocock-skills" / ("issue-tracker-" + tracker + ".md")).read_text(encoding="utf-8")
            for required in ("read-only history recovery only", "not for new MCP Maps",
                             "not be used as a new-Map initializer during installation",
                             "do not execute the historical create/claim/resolve/write commands"):
                self.assertIn(required, text)
            self.assertNotIn("Used by ", text)


class PlatformParity(unittest.TestCase):
    SHARED = (
        "README.md", "windows/README.md", "macos/README.md",
        "html-plan/SKILL.md",
        "wayfinder/SKILL.md",
        "setup-matt-pocock-skills/issue-tracker-github.md",
        "setup-matt-pocock-skills/issue-tracker-gitlab.md",
        "setup-matt-pocock-skills/issue-tracker-local.md",
        "orca-issue-orchestration/scripts/clarification_flow_test.py",
        "orca-issue-orchestration/SKILL.md",
        "orca-issue-orchestration/references/preparation.md",
        "orca-issue-orchestration/references/requirements.md",
        "orca-issue-orchestration/references/operations.md",
        "orca-issue-orchestration/templates/project-config.md",
        "orca-issue-orchestration/templates/issues/user-decision.md",
        "orca-issue-orchestration/templates/issues/LABELS.md",
        "align-requirements/SKILL.md",
        "prd-writing/SKILL.md",
        "orca-issue-orchestration/scripts/mcp_gate.py",
        "orca-issue-orchestration/scripts/mcp_gate_test.py",
    )

    def test_windows_and_macos_ship_identical_content(self):
        for relative in self.SHARED:
            with self.subTest(file=relative):
                if relative.endswith("README.md"):
                    # Keep platform instructions distinct; compare shared contract bytes.
                    marker = "## 工作流 Map 门禁\n\n".encode("utf-8")
                    root = (SUITE / "README.md").read_bytes().split(marker, 1)[1].split(b"\n## ", 1)[0]
                    actual = (SUITE / relative).read_bytes().split(marker, 1)[1].split(b"\n## ", 1)[0]
                    self.assertEqual(root, actual)
                else:
                    self.assertEqual((SUITE / "windows/skills" / relative).read_bytes(),
                                     (SUITE / "macos/skills" / relative).read_bytes())

    def test_gate_script_stays_a_pure_validator(self):
        source = (SCRIPT_DIR / "mcp_gate.py").read_text(encoding="utf-8")
        for forbidden in ("urllib", "requests", "http:", "subprocess", "sqlite"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
