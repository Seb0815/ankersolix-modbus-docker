"""Fetch and validate the Home Assistant independent official Modbus core."""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import cast

REPOSITORY = "anker-charging/ha-anker-solix-official"
API_BASE = f"https://api.github.com/repos/{REPOSITORY}"
ASSET_NAME = "anker_solix_official.zip"
MAX_DOWNLOAD_BYTES = 10 * 1024 * 1024
TAG_PATTERN = re.compile(r"^v?\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?$")
SHA256_PATTERN = re.compile(r"^sha256:([0-9a-f]{64})$")

CORE_MODULE_SYMBOLS: dict[str, frozenset[str]] = {
    "batch_reader.py": frozenset({"BatchRegisterReader"}),
    "config_utils.py": frozenset({"parse_device_configuration"}),
    "const.py": frozenset({"SCAN_INTERVAL", "BATCH_READ_GAP_THRESHOLD", "MAX_REGISTERS_PER_READ"}),
    "device_config.py": frozenset({"AnkerSolixDeviceConfig"}),
    "device_logger.py": frozenset({"WriteResult"}),
    "modbus_client.py": frozenset({"AnkerSolixModbusClient"}),
    "modbus_manager.py": frozenset({"ModbusConnectionManager"}),
}
CORE_MODULE_NAMES = frozenset(Path(name).stem for name in CORE_MODULE_SYMBOLS)
REQUIRED_DATA_FILES = frozenset({"translations/de.json", "translations/en.json"})


class UpstreamContractError(RuntimeError):
    """Raised when an official release no longer matches the supported contract."""


@dataclass(frozen=True)
class Release:
    tag: str
    asset_url: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Provenance:
    repository: str
    tag: str
    version: str
    asset: str
    sha256: str


HttpGetter = Callable[[str], bytes]


def download_bytes(url: str) -> bytes:
    """Download a bounded HTTPS resource."""
    request = urllib.request.Request(url, headers={"User-Agent": "anker-solix-mqtt"})
    with urllib.request.urlopen(request, timeout=30) as response:  # noqa: S310
        content = response.read(MAX_DOWNLOAD_BYTES + 1)
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise UpstreamContractError(f"download exceeds {MAX_DOWNLOAD_BYTES} bytes")
    return content


def resolve_release(tag: str | None, get_bytes: HttpGetter = download_bytes) -> Release:
    """Resolve either the latest stable release or an explicit rollback tag."""
    if tag is not None and not TAG_PATTERN.fullmatch(tag):
        raise UpstreamContractError(f"invalid release tag: {tag!r}")
    endpoint = f"{API_BASE}/releases/latest" if tag is None else f"{API_BASE}/releases/tags/{tag}"
    payload = _json_object(get_bytes(endpoint), "release metadata")
    release_tag = _required_string(payload, "tag_name")
    if not TAG_PATTERN.fullmatch(release_tag):
        raise UpstreamContractError(f"invalid release tag from GitHub: {release_tag!r}")
    if tag is not None and release_tag != tag:
        raise UpstreamContractError(f"requested {tag}, GitHub returned {release_tag}")

    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpstreamContractError("release metadata has no assets list")
    asset_items = cast(list[object], assets)
    matches: list[dict[str, object]] = []
    for item in asset_items:
        if not isinstance(item, dict):
            continue
        asset = cast(dict[str, object], item)
        if asset.get("name") == ASSET_NAME:
            matches.append(asset)
    if len(matches) != 1:
        raise UpstreamContractError(f"release must contain exactly one {ASSET_NAME}")

    asset = matches[0]
    digest = _required_string(asset, "digest")
    digest_match = SHA256_PATTERN.fullmatch(digest)
    if digest_match is None:
        raise UpstreamContractError("release asset has no valid SHA-256 digest")
    size = asset.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or not 0 < size <= MAX_DOWNLOAD_BYTES:
        raise UpstreamContractError("release asset has an invalid size")
    asset_url = _required_string(asset, "browser_download_url")
    expected_prefix = f"https://github.com/{REPOSITORY}/releases/download/"
    if not asset_url.startswith(expected_prefix):
        raise UpstreamContractError("release asset URL is not an official GitHub download")
    return Release(release_tag, asset_url, digest_match.group(1), size)


def fetch_upstream(
    destination: Path,
    tag: str | None = None,
    get_bytes: HttpGetter = download_bytes,
) -> Provenance:
    """Fetch, validate, and replace the vendored package directory."""
    release = resolve_release(tag, get_bytes)
    archive = get_bytes(release.asset_url)
    if len(archive) != release.size:
        raise UpstreamContractError(
            f"asset size mismatch: expected {release.size}, downloaded {len(archive)}"
        )
    actual_sha256 = hashlib.sha256(archive).hexdigest()
    if actual_sha256 != release.sha256:
        raise UpstreamContractError("release asset SHA-256 mismatch")

    license_url = f"https://raw.githubusercontent.com/{REPOSITORY}/{release.tag}/LICENSE"
    license_text = get_bytes(license_url)
    if not license_text.startswith(b"MIT License\n"):
        raise UpstreamContractError("upstream license is missing or no longer MIT")

    selected, version = validate_archive(archive, release.tag)
    provenance = Provenance(REPOSITORY, release.tag, version, ASSET_NAME, actual_sha256)
    _replace_destination(destination, selected, license_text, provenance)
    return provenance


def validate_archive(archive: bytes, release_tag: str) -> tuple[dict[str, bytes], str]:
    """Validate an official integration ZIP and return only standalone files."""
    try:
        with zipfile.ZipFile(io.BytesIO(archive)) as bundle:
            names = bundle.namelist()
            if len(names) != len(set(names)):
                raise UpstreamContractError("release archive contains duplicate paths")
            required = {*CORE_MODULE_SYMBOLS, *REQUIRED_DATA_FILES, "manifest.json"}
            missing = required.difference(names)
            if missing:
                missing_names = ", ".join(sorted(missing))
                raise UpstreamContractError(f"release archive is missing: {missing_names}")
            config_names = sorted(
                name
                for name in names
                if _is_safe_config_path(name) and not bundle.getinfo(name).is_dir()
            )
            if not config_names:
                raise UpstreamContractError("release archive contains no device profiles")
            selected_names = [
                *sorted(CORE_MODULE_SYMBOLS),
                *sorted(REQUIRED_DATA_FILES),
                *config_names,
            ]
            selected = {name: bundle.read(name) for name in selected_names}
            manifest_bytes = bundle.read("manifest.json")
    except (zipfile.BadZipFile, KeyError, OSError) as exc:
        raise UpstreamContractError("release asset is not a valid integration ZIP") from exc

    version = _validate_manifest(manifest_bytes, release_tag)
    for module_name, symbols in CORE_MODULE_SYMBOLS.items():
        _validate_module(module_name, selected[module_name], symbols)
    return selected, version


def _validate_manifest(content: bytes, release_tag: str) -> str:
    manifest = _json_object(content, "manifest")
    version = _required_string(manifest, "version")
    if version != release_tag.removeprefix("v"):
        raise UpstreamContractError(
            f"manifest version {version!r} does not match release {release_tag!r}"
        )
    requirements = manifest.get("requirements")
    if not isinstance(requirements, list):
        raise UpstreamContractError("manifest requirements are invalid")
    requirement_items = cast(list[object], requirements)
    if not all(isinstance(item, str) for item in requirement_items):
        raise UpstreamContractError("manifest requirements are invalid")
    normalized = [cast(str, item).lower() for item in requirement_items]
    for package in ("pymodbus", "pyyaml"):
        if not any(item.startswith(package) for item in normalized):
            raise UpstreamContractError(f"manifest no longer requires {package}")
    return version


def _validate_module(module_name: str, content: bytes, required_symbols: frozenset[str]) -> None:
    try:
        tree = ast.parse(content, filename=module_name)
    except (SyntaxError, UnicodeDecodeError) as exc:
        raise UpstreamContractError(f"cannot parse {module_name}") from exc

    symbols = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }
    symbols.update(
        target.id
        for node in tree.body
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        for target in _assignment_targets(node)
        if isinstance(target, ast.Name)
    )
    missing = required_symbols.difference(symbols)
    if missing:
        raise UpstreamContractError(
            f"{module_name} is missing symbols: {', '.join(sorted(missing))}"
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported = {alias.name.split(".", 1)[0] for alias in node.names}
            if "homeassistant" in imported:
                raise UpstreamContractError(f"{module_name} imports Home Assistant")
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".", 1)[0] == "homeassistant":
                raise UpstreamContractError(f"{module_name} imports Home Assistant")
            if node.level == 1 and node.module and node.module not in CORE_MODULE_NAMES:
                raise UpstreamContractError(
                    f"{module_name} requires unvendored module {node.module!r}"
                )


def _assignment_targets(node: ast.Assign | ast.AnnAssign) -> list[ast.expr]:
    if isinstance(node, ast.Assign):
        return node.targets
    return [node.target]


def _replace_destination(
    destination: Path,
    selected: dict[str, bytes],
    license_text: bytes,
    provenance: Provenance,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        for relative_name, content in selected.items():
            output = staging.joinpath(*PurePosixPath(relative_name).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(content)
        staging.joinpath("__init__.py").write_text(
            '"""Validated Modbus core from the official Anker SOLIX integration."""\n',
            encoding="utf-8",
        )
        staging.joinpath("UPSTREAM_LICENSE").write_bytes(license_text)
        staging.joinpath("_upstream.json").write_text(
            json.dumps(asdict(provenance), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        if destination.exists():
            shutil.rmtree(destination)
        os.replace(staging, destination)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def _json_object(content: bytes, label: str) -> dict[str, object]:
    try:
        parsed: object = json.loads(content)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise UpstreamContractError(f"{label} is not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise UpstreamContractError(f"{label} is not a JSON object")
    return cast(dict[str, object], parsed)


def _required_string(data: dict[str, object], key: str) -> str:
    value = data.get(key)
    if not isinstance(value, str) or not value:
        raise UpstreamContractError(f"missing string field {key!r}")
    return value


def _is_safe_config_path(name: str) -> bool:
    path = PurePosixPath(name)
    return len(path.parts) == 2 and path.parts[0] == "config" and path.suffix == ".yaml"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--tag",
        default=os.environ.get("ANKER_SOLIX_UPSTREAM_TAG") or None,
        help="Release tag for rollback; defaults to the latest stable release",
    )
    parser.add_argument(
        "--destination",
        type=Path,
        default=Path(__file__).parent / "vendor" / "anker_solix_official",
    )
    args = parser.parse_args(argv)
    provenance = fetch_upstream(args.destination, args.tag)
    print(f"Fetched {provenance.repository} {provenance.tag} ({provenance.sha256})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
