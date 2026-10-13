"""Process-wide guard for the documented unittest discovery command."""

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

for name in ("PCO_APP_ID", "PCO_SECRET"):
    os.environ.pop(name, None)

scratch = Path(__file__).resolve().parents[1] / ".scratch" / "tests"
scratch.mkdir(parents=True, exist_ok=True)
tempfile.tempdir = str(scratch)

# Keep these patches alive for the entire test process, including every test module.
network_guards = [patch(target, side_effect=AssertionError("Tests must not use the network"))
                  for target in ("socket.socket.connect", "socket.socket.connect_ex", "socket.getaddrinfo")]
for guard in network_guards:
    guard.start()
