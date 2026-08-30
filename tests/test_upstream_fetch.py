from __future__ import annotations

import hashlib
import io
import json
import zipfile
from pathlib import Path

import pytest

from anker_solix_mqtt.upstream_fetch import (
    API_BASE,
    ASSET_NAME,
    CORE_MODULE_SYMBOLS,
    REPOSITORY,
    REQUIRED_DATA_FILES,
    UpstreamContractError,
    fetch_upstream,
)

TAG = "v1.4.3"
ASSET_URL = f"https://github.com/{REPOSITORY}/releases/download/{TAG}/{ASSET_NAME}"


def _module_source(symbols: frozenset[str]) -> str:
    lines: list[str] = []
    for symbol in sorted(symbols):
        if symbol.isupper():
            lines.append(f"{symbol} = 1")
        elif symbol == "parse_device_configuration":
            lines.append(f"def {symbol}():\n    return {{}}, []")
        else:
            lines.append(f"class {symbol}:\n    pass")
    return "\n\n".join(lines) + "\n"


def _archive(module_override: tuple[str, str] | None = None) -> bytes:
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as bundle:
        for name, symbols in CORE_MODULE_SYMBOLS.items():
            source = (
                module_override[1]
                if module_override and module_override[0] == name
                else _module_source(symbols)
            )
            bundle.writestr(name, source)
        for name in REQUIRED_DATA_FILES:
            bundle.writestr(name, '{"entity": {}}')
        bundle.writestr("config/device.yaml", "product_info:\n  name: Test\n")
        bundle.writestr(
            "manifest.json",
            json.dumps(
                {
                    "version": "1.4.3",
                    "requirements": ["pymodbus>=3.10,<4", "PyYAML>=5.4"],
                }
            ),
        )
        bundle.writestr("coordinator.py", "from homeassistant.core import HomeAssistant\n")
    return output.getvalue()


def _getter(archive: bytes, digest: str | None = None):
    release = json.dumps(
        {
            "tag_name": TAG,
            "assets": [
                {
                    "name": ASSET_NAME,
                    "digest": f"sha256:{digest or hashlib.sha256(archive).hexdigest()}",
                    "size": len(archive),
                    "browser_download_url": ASSET_URL,
                }
            ],
        }
    ).encode()

    def get_bytes(url: str) -> bytes:
        if url == f"{API_BASE}/releases/latest":
            return release
        if url == ASSET_URL:
            return archive
        if url == f"https://raw.githubusercontent.com/{REPOSITORY}/{TAG}/LICENSE":
            return b"MIT License\n\nCopyright (c) Anker\n"
        raise AssertionError(f"unexpected URL: {url}")

    return get_bytes


def test_fetches_validated_core_and_records_provenance(tmp_path: Path) -> None:
    archive = _archive()
    destination = tmp_path / "official"

    provenance = fetch_upstream(destination, get_bytes=_getter(archive))

    assert provenance.tag == TAG
    assert (destination / "modbus_client.py").is_file()
    assert (destination / "config" / "device.yaml").is_file()
    assert (destination / "translations" / "de.json").is_file()
    assert not (destination / "coordinator.py").exists()
    metadata = json.loads((destination / "_upstream.json").read_text())
    assert metadata["sha256"] == hashlib.sha256(archive).hexdigest()
    assert (destination / "UPSTREAM_LICENSE").read_text().startswith("MIT License")


def test_rejects_asset_with_wrong_digest(tmp_path: Path) -> None:
    archive = _archive()

    with pytest.raises(UpstreamContractError, match="SHA-256 mismatch"):
        fetch_upstream(tmp_path / "official", get_bytes=_getter(archive, "0" * 64))


def test_rejects_new_dependency_on_home_assistant(tmp_path: Path) -> None:
    archive = _archive(
        (
            "modbus_client.py",
            "from homeassistant.core import HomeAssistant\n"
            "class AnkerSolixModbusClient:\n"
            "    pass\n",
        )
    )

    with pytest.raises(UpstreamContractError, match="imports Home Assistant"):
        fetch_upstream(tmp_path / "official", get_bytes=_getter(archive))
