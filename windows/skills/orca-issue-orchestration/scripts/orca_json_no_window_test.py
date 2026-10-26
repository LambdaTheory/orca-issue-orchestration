"""T1: exact subprocess arguments and existing envelopes on both platforms."""

import subprocess
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import orch_guard
import orch_wait


class NoWindow(unittest.TestCase):
    def check_case(self, module, platform, stdout="", stderr="", returncode=0,
                   expected=None, error=None):
        argv = ["terminal", "list", "--json"]
        command = [orch_guard.ORCA, *argv] if module is orch_guard else ["orca-test", *argv]
        # Replace only the module's OS reference, never the host's os.name/Path.
        with patch.object(module, "os", SimpleNamespace(name=platform)), \
                patch.object(subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), \
                patch.object(subprocess, "run", side_effect=error,
                             return_value=SimpleNamespace(stdout=stdout, stderr=stderr,
                                                          returncode=returncode)) as run:
            result = module.orca_json(argv if module is orch_guard else command, timeout=17)
        options = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                       text=True, encoding="utf-8", timeout=17)
        if platform == "nt":
            options["creationflags"] = 0x08000000
        run.assert_called_once_with(command, **options)
        self.assertEqual(result, expected)

    def test_platform_and_response_matrix(self):
        for module in (orch_guard, orch_wait):
            guard = module is orch_guard
            cases = [
                dict(stdout=' {"ok":true,"result":{"text":"中文"}} \n',
                     expected={"ok": True, "result": {"text": "中文"}}),
                dict(stdout='{"ok":true}', stderr=" failed ", returncode=7,
                     expected={"ok": True} if guard else {
                         "ok": False, "error": {"code": "check_failed", "message": "failed"},
                         "response": {"ok": True}}),
                dict(stdout='{"ok":false,"error":{"code":"original"}}', returncode=7,
                     expected={"ok": False, "error": {"code": "original"}}),
                dict(stderr=" empty detail ", expected={"ok": False, "error": {
                    "code": "empty" if guard else "bad_json", "message": "empty detail"}}),
                dict(returncode=7, expected={"ok": False, "error": {
                    "code": "empty" if guard else "check_failed", "message": "exit 7"}}),
                dict(stdout=" invalid json ", expected={"ok": False, "error": {
                    "code": "bad_json", "message": "invalid json"}}),
                dict(error=subprocess.TimeoutExpired("orca-test", 17),
                     expected={"ok": False, "error": {"code": "cli_timeout", "message":
                         "orca terminal list timed out" if guard else
                         "CLI request timed out; reconcile before retrying any ACK"}}),
                dict(error=FileNotFoundError("missing CLI"), expected={"ok": False,
                     "error": {"code": "cli_missing" if guard else "check_failed",
                               "message": "missing CLI"}}),
            ]
            for platform in ("nt", "posix"):
                for index, case in enumerate(cases):
                    with self.subTest(module=module.__name__, platform=platform, case=index):
                        self.check_case(module, platform, **case)


if __name__ == "__main__":
    unittest.main()
