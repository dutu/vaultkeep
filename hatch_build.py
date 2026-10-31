"""Embed the source commit in wheels and source archives."""

import runpy
import tempfile
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    def initialize(self, version, build_data):
        root = Path(self.root)
        source_commit = runpy.run_path(str(root / "src/vaultkeep/version.py"))["source_commit"]
        self.commit_directory = tempfile.TemporaryDirectory(prefix="vaultkeep-commit-")
        marker = Path(self.commit_directory.name) / "_commit.txt"
        marker.write_text(source_commit(root) + "\n", encoding="ascii")
        destination = (
            "src/vaultkeep/_commit.txt" if self.target_name == "sdist" else "vaultkeep/_commit.txt"
        )
        build_data["force_include"][str(marker)] = destination

    def finalize(self, version, build_data, artifact_path):
        self.commit_directory.cleanup()
