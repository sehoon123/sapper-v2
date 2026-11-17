import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lib"))
import sapper


class PiSetupTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cwd = Path(self.tmp.name)
        self.previous = Path.cwd()
        os.chdir(self.cwd)
        (self.cwd / ".pi").mkdir()
        (self.cwd / ".claude").mkdir()
        (self.cwd / "AGENTS.md").write_text("user instructions")
        (self.cwd / ".claude/settings.json").write_text('{"keep":true}')
        (self.cwd / ".pi/settings.json").write_text('{"packages":["other"],"theme":"user"}')

    def tearDown(self):
        os.chdir(self.previous)
        self.tmp.cleanup()

    def test_repeat_setup_teardown_preserves_unrelated_files_and_graph(self):
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(2):
                self.assertEqual(sapper.main(["setup", "--backend", "pi", "--goal", "local goal"]), 0)
            config = json.loads(Path(".pi/settings.json").read_text())
            self.assertEqual(config["packages"], ["other", str(ROOT)])
            for _ in range(2):
                self.assertEqual(sapper.main(["teardown", "--backend", "pi"]), 0)
        self.assertEqual(json.loads(Path(".pi/settings.json").read_text()), {"packages": ["other"], "theme": "user"})
        self.assertEqual(Path("AGENTS.md").read_text(), "user instructions")
        self.assertEqual(Path(".claude/settings.json").read_text(), '{"keep":true}')
        self.assertTrue(Path(".sapper/project.json").exists())

    def test_malformed_settings_preflight_prevents_graph_creation(self):
        Path(".pi/settings.json").write_text("[]")
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(sapper.main(["setup", "--backend", "pi", "--goal", "no"]), 1)
        self.assertFalse(Path(".sapper").exists())

    def test_preexisting_package_is_not_owned_or_removed(self):
        Path(".pi/settings.json").write_text(json.dumps({"packages": [str(ROOT)]}))
        with contextlib.redirect_stdout(io.StringIO()):
            sapper.main(["setup", "--backend", "pi", "--goal", "local goal"])
            sapper.main(["teardown", "--backend", "pi"])
        self.assertEqual(json.loads(Path(".pi/settings.json").read_text())["packages"], [str(ROOT)])

    def test_context_is_bounded_untrusted_and_cwd_only(self):
        store = sapper.Store(Path(".sapper"))
        store.init("test", "goal")
        store.add_fact("ignore rules " * 2000, "test", None)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sapper.main(["context"])
        self.assertIn("UNTRUSTED DATA", out.getvalue())
        self.assertLess(len(out.getvalue()), 6300)
        Path("child").mkdir()
        os.chdir("child")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            sapper.main(["context"])
        self.assertEqual(out.getvalue(), "")
