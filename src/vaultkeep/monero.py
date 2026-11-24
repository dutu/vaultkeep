"""Wallet selection evidence for Monero Wallet GUI and RPC."""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from vaultkeep.application import ProcessInfo, ProcessProvider


def _options(arguments: tuple[str, ...]) -> dict[str, str]:
    """Read wallet selection arguments after argv[0] and reject invalid selections."""
    result: dict[str, str] = {}
    names = {"--config-file", "--wallet-file", "--wallet-dir", "--generate-from-json"}
    for index, token in enumerate(arguments):
        key, separator, value = token.partition("=")
        if key not in names:
            continue
        if key in result or key in ("--wallet-dir", "--generate-from-json"):
            raise ValueError("conflicting or unsupported wallet selection")
        if not separator:
            value = arguments[index + 1] if index + 1 < len(arguments) else ""
        if not value or value.startswith("--"):
            raise ValueError("missing selection value")
        result[key] = value
    return result


def _qt_string(value: str) -> str:
    """Decode scalar QString INI values, without Qt object deserialization."""
    quoted = value.startswith('"')
    if quoted:
        if not value.endswith('"') or len(value) < 2:
            raise ValueError("unterminated string")
        value = value[1:-1]
    elif "," in value or '"' in value:
        raise ValueError("not a scalar string")
    output: list[str] = []
    index = 0
    escapes = {
        "a": "\a",
        "b": "\b",
        "f": "\f",
        "n": "\n",
        "r": "\r",
        "t": "\t",
        "v": "\v",
        "\\": "\\",
        '"': '"',
    }
    while index < len(value):
        char = value[index]
        index += 1
        if char != "\\":
            if char == '"':
                raise ValueError("unescaped quote")
            output.append(char)
            continue
        if index == len(value):
            raise ValueError("incomplete escape")
        escape = value[index]
        index += 1
        if escape == "x":
            match = re.match(r"[0-9a-fA-F]{1,4}", value[index:])
            if match is None:
                raise ValueError("invalid hex escape")
            output.append(chr(int(match[0], 16)))
            index += len(match[0])
        elif escape in escapes:
            output.append(escapes[escape])
        else:
            raise ValueError("unsupported escape")
    # Qt INI escapes represent UTF-16 code units, including surrogate pairs.
    # Strict decoding combines valid pairs and rejects lone/reversed surrogates.
    result = "".join(output).encode("utf-16-le", "surrogatepass").decode("utf-16-le")
    if result.startswith("@@"):
        result = result[1:]
    elif result.startswith("@"):
        raise ValueError("unsupported Qt encoding")
    if not result or any(ord(char) < 32 for char in result):
        raise ValueError("invalid path string")
    return result


def _wallet_setting(text: str, *, gui: bool) -> str | None:
    section = "General"
    value: str | None = None
    relevant = "wallet_path" if gui else "wallet-file"
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";") if gui else ("#",)):
            continue
        if gui and line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            continue
        if not gui:
            line = line.split("#", 1)[0].strip()
        key, separator, item = line.partition("=")
        if not separator:
            raise ValueError("invalid setting")
        key, item = key.strip(), item.strip()
        if not gui and key in ("wallet-dir", "generate-from-json"):
            raise ValueError("unsupported wallet selection")
        if key == relevant and (not gui or section == "General"):
            if value is not None or not item:
                raise ValueError("duplicate or empty wallet setting")
            value = _qt_string(item) if gui else item
    return value


def _path(pid: int, value: str, provider: ProcessProvider) -> str | None:
    # These are application paths, not shell expressions (including ~).
    return provider.resolve_directory(pid, value if os.path.isabs(value) else "./" + value)


def _read(pid: int, value: str, provider: ProcessProvider) -> tuple[str | None, str]:
    path = _path(pid, value, provider)
    if path is None:
        return None, "inaccessible"
    return provider.read_text_file(pid, path)


class MoneroAdapter:
    env_keys: frozenset[str] = frozenset({"HOME", "XDG_CONFIG_HOME"})

    def resolve_directory(
        self, role: str, proc: ProcessInfo, provider: ProcessProvider
    ) -> tuple[str | None, str]:
        try:
            if role == "monero-wallet-gui":
                wallet, status = self._gui_wallet(proc, provider)
            else:
                wallet, status = self._rpc_wallet(proc, provider)
            if wallet is None:
                return None, "unresolved" if status == "missing" else status
            resolved = _path(proc.pid, wallet, provider)
            if resolved is None:
                return None, "inaccessible"
            return os.path.dirname(resolved), "resolved"
        except (ValueError, UnicodeError):
            return None, "unresolved"

    def _gui_wallet(self, proc: ProcessInfo, provider: ProcessProvider) -> tuple[str | None, str]:
        text, status = self._gui_settings(proc, provider)
        if text is None:
            return None, status
        wallet = _wallet_setting(text, gui=True)
        return wallet, "resolved" if wallet else "unresolved"

    def _gui_settings(self, proc: ProcessInfo, provider: ProcessProvider) -> tuple[str | None, str]:
        marker, status = _read(proc.pid, "monero-storage/.portable", provider)
        if marker is None and status != "missing":
            return None, status
        if marker is None or marker.strip(" \t\n\r\v\f") != "disabled":
            text, status = _read(proc.pid, "monero-storage/settings.ini", provider)
            # Without a marker, the settings file itself identifies legacy portable mode.
            if marker is not None or status != "missing":
                return text, status

        env = provider.read_env(proc.pid, self.env_keys)
        if env is None:
            return None, "inaccessible"
        base = env.get("XDG_CONFIG_HOME")
        if not base:
            home = env.get("HOME", "")
            if not os.path.isabs(home):
                return None, "inaccessible"
            base = os.path.join(home, ".config")
        if not os.path.isabs(base):
            return None, "unresolved"
        return _read(proc.pid, os.path.join(base, "monero-project/monero-core.conf"), provider)

    def _rpc_wallet(self, proc: ProcessInfo, provider: ProcessProvider) -> tuple[str | None, str]:
        if not proc.cmdline_available:
            return None, "inaccessible"
        options = _options(proc.cmdline[1:])
        wallet = options.get("--wallet-file")
        if "--config-file" in options:
            text, status = _read(proc.pid, options["--config-file"], provider)
            if text is None:
                return None, "unresolved" if status == "missing" else status
            # Inspect the config for unsupported selection modes even with an explicit wallet.
            configured = _wallet_setting(text, gui=False)
            wallet = wallet or configured
        return wallet, "resolved" if wallet else "unresolved"
