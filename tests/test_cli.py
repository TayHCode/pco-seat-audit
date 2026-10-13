import hermetic  # Block live connections and discard inherited credentials.
import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pco_seat_audit.cli import main

ROOT = Path(__file__).resolve().parents[1]


class CliTests(unittest.TestCase):
    def test_live_requires_credentials_without_attempting_network(self):
        with tempfile.TemporaryDirectory() as folder:
            stderr = io.StringIO()
            with patch.dict("os.environ", {}, clear=True), patch("pco_seat_audit.cli.urlopen") as transport, contextlib.redirect_stderr(stderr):
                result = main(["--live", "--plan", str(ROOT / "examples/plan.json"), "--output", str(Path(folder) / "report")])
            transport.assert_not_called()
            self.assertEqual(result, 2)
            self.assertEqual(stderr.getvalue().strip(), "error: live mode needs PCO_APP_ID and PCO_SECRET environment variables")


if __name__ == "__main__":
    unittest.main()
