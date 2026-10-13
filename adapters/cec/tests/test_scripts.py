"""Read-only script/packaging tests; fixture ELF headers are NOT device evidence."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]


def run(script: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(ROOT / "scripts" / script), *arguments],
        text=True,
        capture_output=True,
        check=False,
    )


@pytest.fixture
def artifacts(tmp_path: Path) -> tuple[Path, Path, Path]:
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    # deliberately synthetic metadata for validation tests, never hardware execution.
    header = b"\x7fELF\x02\x01" + b"\0" * 12 + (183).to_bytes(2, "little")
    (runtime / "flutter-pi").write_bytes(header)
    (runtime / "flutter-pi").chmod(0o755)
    (runtime / "libflutter_engine.so").write_bytes(header)
    (runtime / "icudtl.dat").write_bytes(b"fixture ICU")
    value = {
        "flutter_version": "3.38.10",
        "flutterpi_tool_version": "0.10.1",
        "framework_revision": "a" * 40,
        "engine_revision": "b" * 40,
        "flutter_pi_revision": "c" * 40,
    }
    for filename, key in (
        ("flutter-pi", "runtime_sha256"),
        ("libflutter_engine.so", "engine_sha256"),
        ("icudtl.dat", "icu_sha256"),
    ):
        value[key] = hashlib.sha256((runtime / filename).read_bytes()).hexdigest()
    tuple_file = tmp_path / "tuple.json"
    tuple_file.write_text(json.dumps(value))
    bundle = tmp_path / "bundle"
    (bundle / "app/assets/contracts").mkdir(parents=True)
    for artifact in ("libflutter_engine.so", "icudtl.dat"):
        shutil.copyfile(runtime / artifact, bundle / "app" / artifact)
    (bundle / "app/app.so").write_bytes(header)
    (bundle / "app/assets/contracts/dashboard.v1.schema.json").write_text("{}")
    (bundle / "bundle.json").write_text(json.dumps({"hardware_validated": False, "tuple": value}))
    return tuple_file, runtime, bundle


def test_missing_tuple_runtime_fails_clearly() -> None:
    for script in ("build-app.sh", "provision.sh"):
        result = run(script, "--dry-run")
        assert result.returncode != 0
        assert "explicit --tuple" in result.stderr


def test_tuple_validation_and_tampering(artifacts: tuple[Path, Path, Path]) -> None:
    tuple_file, runtime, _ = artifacts
    result = run(
        "build-app.sh", "--validate-only", "--tuple", str(tuple_file), "--runtime", str(runtime)
    )
    assert result.returncode == 0
    assert "compatibility remains untested" in result.stdout
    (runtime / "libflutter_engine.so").write_bytes(b"wrong artifact")
    result = run(
        "build-app.sh", "--validate-only", "--tuple", str(tuple_file), "--runtime", str(runtime)
    )
    assert result.returncode != 0


def test_dry_run_does_not_write_or_execute_build(
    artifacts: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    tuple_file, runtime, _ = artifacts
    sdk = tmp_path / "sdk"
    (sdk / "bin").mkdir(parents=True)
    for name in ("flutter", "tool"):
        path = sdk / "bin" / name
        path.write_text("#!/bin/sh\nexit 99\n")
        path.chmod(0o755)
    output = tmp_path / "output"
    result = run(
        "build-app.sh",
        "--dry-run",
        "--tuple",
        str(tuple_file),
        "--runtime",
        str(runtime),
        "--flutter-sdk",
        str(sdk),
        "--flutterpi-tool",
        str(sdk / "bin/tool"),
        "--bundle-source",
        str(tmp_path / "not-yet-built"),
        "--output",
        str(output),
    )
    assert result.returncode == 0 and "DRY RUN" in result.stdout
    assert not output.exists()


def test_provision_dry_run_and_apply_guard(artifacts: tuple[Path, Path, Path]) -> None:
    tuple_file, runtime, _ = artifacts
    args = ("--tuple", str(tuple_file), "--runtime", str(runtime))
    result = run("provision.sh", "--dry-run", *args)
    assert result.returncode == 0
    assert "no services started" in result.stdout
    result = run("provision.sh", "--apply", *args)
    assert result.returncode != 0
    assert "--allow-system-changes" in result.stderr
    result = run("provision.sh", "--dry-run", *args, "--drm-device", "/dev/uinput")
    assert result.returncode != 0


def test_deploy_dry_run_and_unsafe_version(
    artifacts: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    tuple_file, runtime, bundle = artifacts
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\nexit 99\n")
    uv.chmod(0o755)
    args = (
        "--tuple",
        str(tuple_file),
        "--runtime",
        str(runtime),
        "--bundle",
        str(bundle),
        "--uv",
        str(uv),
        "--source",
        str(ROOT),
    )
    result = run("deploy.sh", "--dry-run", "--version", "adapter-test", *args)
    assert result.returncode == 0 and "DRY RUN" in result.stdout
    result = run("deploy.sh", "--dry-run", "--version", "../../etc", *args)
    assert result.returncode != 0
    assert "safe --version" in result.stderr
    (bundle / "app/app.so").write_bytes(b"x64 or corrupt")
    result = run("deploy.sh", "--dry-run", "--version", "adapter-test", *args)
    assert result.returncode != 0


def test_doctor_dry_run_and_script_syntax() -> None:
    assert run("doctor.sh", "--dry-run").returncode == 0
    for script in ("provision.sh", "build-app.sh", "deploy.sh", "doctor.sh"):
        result = subprocess.run(["bash", "-n", str(ROOT / "scripts" / script)], check=False)
        assert result.returncode == 0


def test_units_verify_in_isolated_structure_only(tmp_path: Path) -> None:
    executable = shutil.which("systemd-analyze")
    if executable is None:
        pytest.skip("systemd-analyze unavailable")
    # stubs satisfy verify's path checks; they are never run or installed on the host.
    for path in ("opt/aos/current/.venv/bin/python", "opt/aos/current/runtime/flutter-pi"):
        file = tmp_path / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("structural verification stub, not an appliance runtime\n")
        file.chmod(0o755)
    units = tmp_path / "etc/systemd/system"
    units.mkdir(parents=True)
    for name in (
        "sysinit.target",
        "basic.target",
        "shutdown.target",
        "multi-user.target",
        "local-fs.target",
        "network-online.target",
    ):
        (units / name).write_text("[Unit]\nDescription=isolated verification target\n")
    (units / "systemd-udev-settle.service").write_text(
        "[Service]\nExecStart=/opt/aos/current/.venv/bin/python\n"
    )
    files = list((ROOT / "systemd").glob("aos-*.service"))
    for file in files:
        text = file.read_text()
        for path in ("/opt/aos/current/.venv/bin/python", "/opt/aos/current/runtime/flutter-pi"):
            text = text.replace(path, str(tmp_path) + path)
        (units / file.name).write_text(text)
    (units / "systemd-udev-settle.service").write_text(
        "[Service]\nExecStart=" + str(tmp_path / "opt/aos/current/.venv/bin/python") + "\n"
    )
    result = subprocess.run(
        [executable, "verify", "--man=no", *[str(units / file.name) for file in files]],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "SYSTEMD_UNIT_PATH": str(units)},
    )
    assert result.returncode == 0, result.stderr
