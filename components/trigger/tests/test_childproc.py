from __future__ import annotations

import signal
import sys

from vaultkeep_trigger.childproc import run_child


def test_output_eof_does_not_terminate_unbounded_child() -> None:
    result = run_child(
        [sys.executable, "-c", "import os,time; os.close(1); os.close(2); time.sleep(1.2)"], env={}
    )
    assert result.exit_code == 0 and result.signal_number is None
    assert result.duration_seconds >= 1.2


def test_combined_output_is_capped_and_final_stdout_marker_survives() -> None:
    code = "import os; os.write(2,b'e'*600000); os.write(1,b'o'*600000+b'\\nrun: created\\n')"
    result = run_child([sys.executable, "-c", code], env={}, cap=1024)
    assert result.exit_code == 0
    assert len(result.diagnostics) <= 1024
    assert result.diagnostics_truncated
    assert result.stdout_tail == "run: created"


def test_oversized_last_line_cannot_forge_a_result_marker() -> None:
    result = run_child(
        [sys.executable, "-c", "import os; os.write(1,b' '*100000+b'run: created')"],
        env={},
        cap=1024,
    )
    assert result.stdout_tail is None
    assert len(result.diagnostics) <= 1024


def test_only_explicit_timeout_terminates_preflight_child() -> None:
    result = run_child([sys.executable, "-c", "import time; time.sleep(60)"], env={}, timeout=0.1)
    assert result.signal_number == signal.SIGKILL
    assert result.duration_seconds < 5
