"""Build both distributions and verify isolation, versions and extracted sdists.

Run with the locked development environment and a prepared binary wheelhouse.
All builds/installations use temporary paths and no services or production data.
"""

from __future__ import annotations

import configparser
import email
import os
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(*command: str, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
    return subprocess.check_output(command, cwd=cwd, env=env, text=True, stderr=subprocess.STDOUT)


def build(project: Path, output: Path, kind: str = "") -> None:
    arguments = [sys.executable, "-m", "build", "--no-isolation", "--outdir", str(output)]
    if kind:
        arguments.append("--" + kind)
    run(*arguments, str(project))


def check_wheel(wheel: Path, package: str, command: str, version: str) -> None:
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert {n.split("/", 1)[0] for n in names} <= {
            package,
            wheel.name.removesuffix("-py3-none-any.whl") + ".dist-info",
        }
        metadata_path = next(n for n in names if n.endswith(".dist-info/METADATA"))
        metadata = email.message_from_bytes(archive.read(metadata_path))
        assert metadata["Version"] == version
        assert metadata["Requires-Python"] == ">=3.13"
        dependencies = metadata.get_all("Requires-Dist", [])
        assert any("psutil" in d for d in dependencies) == (command == "vaultkeep-trigger")
        assert not any(d.startswith("vaultkeep") for d in dependencies)
        entry_path = next(n for n in names if n.endswith(".dist-info/entry_points.txt"))
        entries = configparser.ConfigParser()
        entries.read_string(archive.read(entry_path).decode())
        assert set(entries["console_scripts"]) == {command}
        assert any("LICENSE" in n for n in names)


def check() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    component_version = tomllib.loads((ROOT / "components/trigger/pyproject.toml").read_text())[
        "project"
    ]["version"]
    assert version == component_version
    environment = os.environ.copy()
    environment.update(PIP_NO_INDEX="1", PIP_DISABLE_PIP_VERSION_CHECK="1")
    environment.setdefault("PIP_FIND_LINKS", "/tmp/vaultkeep-wheelhouse")
    with tempfile.TemporaryDirectory(prefix="vaultkeep-packages-") as directory:
        base = Path(directory)
        os_release = base / "os-release"
        os_release.write_text("ID=debian\nVERSION_ID=13\nVERSION_CODENAME=trixie\n")
        for project, name, namespace in (
            (ROOT, "vaultkeep", "vaultkeep"),
            (ROOT / "components/trigger", "vaultkeep-trigger", "vaultkeep_trigger"),
        ):
            output = base / name
            build(project, output)
            wheel = next(output.glob("*.whl"))
            check_wheel(wheel, namespace, name, version)
            with tarfile.open(next(output.glob("*.tar.gz"))) as archive:
                members = archive.getnames()
                assert not any(
                    part in n.split("/")
                    for n in members
                    for part in (
                        ".git",
                        ".venv",
                        "__pycache__",
                        ".aws",
                        ".env",
                        ".codex",
                        ".agents",
                    )
                )
                archive.extractall(output / "extracted", filter="data")
            extracted = next((output / "extracted").iterdir())
            build(extracted, output / "rebuilt", "wheel")
            if name == "vaultkeep":
                for required in (
                    "components/trigger/pyproject.toml",
                    "requirements-trigger.lock",
                    "requirements.lock",
                    "scripts/install_support.py",
                    "install.sh",
                    "components/trigger/examples/config.toml",
                ):
                    assert (extracted / required).is_file(), required
                for combined in (False, True):
                    env = dict(
                        environment,
                        VAULTKEEP_INSTALL_TESTING="1",
                        VAULTKEEP_TEST_OS_RELEASE=str(os_release),
                        VAULTKEEP_INSTALL_PREFIX=str(base / "preview/opt/vaultkeep"),
                        VAULTKEEP_CONFIG_ROOT=str(base / "preview/etc/vaultkeep"),
                        VAULTKEEP_VAR_ROOT=str(base / "preview/var/vaultkeep"),
                        VAULTKEEP_BIN_LINK=str(base / "preview/bin/vaultkeep"),
                        VAULTKEEP_TRIGGER_BIN_LINK=str(base / "preview/bin/vaultkeep-trigger"),
                        VAULTKEEP_SYSTEMD_ROOT=str(base / "preview/systemd/system"),
                        VAULTKEEP_USER_SYSTEMD_ROOT=str(base / "preview/systemd/user"),
                    )
                    run(
                        "bash",
                        str(extracted / "install.sh"),
                        "install",
                        "--dry-run",
                        *(["--with-trigger"] if combined else []),
                        env=env,
                        cwd=extracted,
                    )
                    assert not (base / "preview").exists()
            venv = base / (name + "-env")
            run(sys.executable, "-m", "venv", str(venv))
            python = str(venv / "bin/python")
            if name == "vaultkeep":
                run(
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--require-hashes",
                    "--only-binary=:all:",
                    "-r",
                    str(ROOT / "requirements.lock"),
                    env=environment,
                )
            else:
                # Prove the independent wheel installs with only its declared deps.
                run(
                    python,
                    "-m",
                    "pip",
                    "install",
                    "--only-binary=:all:",
                    str(wheel),
                    env=environment,
                )
            run(python, "-m", "pip", "install", "--no-deps", str(wheel), env=environment)
            other = "vaultkeep_trigger" if name == "vaultkeep" else "vaultkeep"
            run(
                python,
                "-c",
                f"import {namespace}; import importlib.util; "
                f"assert importlib.util.find_spec('{other}') is None",
            )
            assert run(str(venv / "bin" / name), "--version").strip() == version
            run(python, "-m", "pip", "check")
        print("Both wheels and independent sdists passed package isolation/version checks.")


if __name__ == "__main__":
    try:
        check()
    except subprocess.CalledProcessError as error:
        print(error.output, file=sys.stderr)
        raise
