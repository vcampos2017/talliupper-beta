from pathlib import Path


ROOT = Path(__file__).parents[1]
LAUNCHER = (ROOT / "start_receipt_lens.bat").read_text(encoding="utf-8")


def test_launcher_selects_a_supported_python_version() -> None:
    assert "py -3.12 --version" in LAUNCHER
    assert "PYTHON_COMMAND=py -3.12" in LAUNCHER
    assert "PYTHON_COMMAND=py -3.11" in LAUNCHER
    assert "%PYTHON_COMMAND% -m venv .venv" in LAUNCHER
    assert "py -m venv .venv" not in LAUNCHER


def test_launcher_recovers_incompatible_and_partial_environments() -> None:
    assert "sys.version_info[:2] in ((3, 11), (3, 12))" in LAUNCHER
    assert 'choice /C YN /M "Delete and rebuild only the project-local .venv folder"' in LAUNCHER
    assert 'rmdir /s /q ".venv"' in LAUNCHER
    assert ".receipt_lens_ready" in LAUNCHER


def test_launcher_checks_core_runtime_imports_before_starting() -> None:
    assert 'import paddle, paddleocr, pypdfium2, pillow_heif, reportlab, streamlit' in LAUNCHER
    assert '".venv\\Scripts\\python.exe" -m streamlit run app.py' in LAUNCHER


def test_launcher_reinstalls_when_requirements_change() -> None:
    # RL-78: an update that adds a package must reinstall once, even when the
    # older environment still imports the original core packages.
    snapshot = (
        'copy /b /y requirements.txt + requirements-local.txt + requirements-core.txt '
        '".venv\\.requirements_snapshot"'
    )
    assert snapshot in LAUNCHER
    assert 'fc /b ".venv\\.requirements_snapshot" ".venv\\.receipt_lens_ready"' in LAUNCHER
    assert 'copy /b /y ".venv\\.requirements_snapshot" ".venv\\.receipt_lens_ready"' in LAUNCHER
    assert 'type nul > ".venv\\.receipt_lens_ready"' not in LAUNCHER


def test_launcher_snapshot_covers_every_file_requirements_txt_pulls_in() -> None:
    included = {"requirements.txt"}
    pending = ["requirements.txt"]
    while pending:
        for line in (ROOT / pending.pop()).read_text(encoding="utf-8").splitlines():
            if line.startswith("-r "):
                name = line[3:].strip()
                if name not in included:
                    included.add(name)
                    pending.append(name)
    for name in included:
        assert name in LAUNCHER


def test_launcher_refuses_to_start_a_second_copy() -> None:
    # RL-79: a second copy could bind 8501 on IPv6 next to one on IPv4.
    check = "Get-NetTCPConnection -LocalPort 8501 -State Listen"
    assert check in LAUNCHER
    assert "TalliUpper already seems to be running on port 8501." in LAUNCHER
    # The check must run before any install or start step.
    assert LAUNCHER.index(check) < LAUNCHER.index("-m venv .venv")
    assert LAUNCHER.index(check) < LAUNCHER.index("-m streamlit run app.py")
