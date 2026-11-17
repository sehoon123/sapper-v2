import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

FIXTURE = Path(__file__).parent / "fixtures" / "fake_pi.py"


class PiFixtureTests(unittest.TestCase):
    def test_lf_framing_and_retry_fixture(self):
        for scenario in ("split", "retry", "stderr", "handled"):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as cwd:
                result = subprocess.run([sys.executable, str(FIXTURE.resolve())], cwd=cwd,
                    input=b'{"id":"p","type":"prompt","message":"test"}\n',
                    capture_output=True, timeout=5, env={**os.environ, "FAKE_PI_SCENARIO": scenario})
                records = [json.loads(line) for line in result.stdout.split(b"\n") if line]
                self.assertEqual(result.returncode, 0)
                self.assertEqual(records[0]["id"], "p")
                if scenario == "handled":
                    self.assertEqual(len(records), 1)
                else:
                    self.assertEqual(records[-1]["type"], "agent_settled")
                    self.assertIn("한글\u2028line\u2029paragraph", records[-3]["message"]["content"][0]["text"])
