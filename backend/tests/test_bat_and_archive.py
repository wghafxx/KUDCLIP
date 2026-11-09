"""
Tests for KUDCLIP ЗАПУСК_WINDOWS.bat encoding fix and /api/source/archive endpoint.
Verifies:
- Local bat file is pure ASCII, no BOM, syntactically balanced.
- /api/source/archive returns 200 zip containing new ASCII bat + key source files.
- Backend root /api/ is alive.
"""
import io
import os
import re
import zipfile
import requests

BASE_URL = os.environ.get("REACT_APP_BACKEND_URL", "https://subtitle-fix-3.preview.emergentagent.com").rstrip("/")
BAT_PATH = "/app/ЗАПУСК_WINDOWS.bat"
BAT_NAME = "ЗАПУСК_WINDOWS.bat"


# --- local bat file checks ---
def test_bat_is_pure_ascii_no_bom():
    data = open(BAT_PATH, "rb").read()
    assert data[:3] != b"\xef\xbb\xbf", "BAT still has UTF-8 BOM"
    non_ascii = [(i, b) for i, b in enumerate(data) if b > 127]
    assert not non_ascii, f"Non-ASCII bytes present: {non_ascii[:5]}"


def test_bat_has_expected_content():
    data = open(BAT_PATH, "rb").read()
    assert b"Installing backend dependencies" in data
    assert b"@echo off" in data


def test_bat_syntax_balanced_parens():
    lines = open(BAT_PATH).read().splitlines()
    depth = 0
    for i, l in enumerate(lines, 1):
        s = l.rstrip()
        if re.search(r"\(\s*$", s):
            if not re.match(r"^\s*\)\s*else\s*\(\s*$", s):
                depth += 1
        elif re.match(r"^\s*\)\s*$", s):
            depth -= 1
        assert depth >= 0, f"Negative depth at line {i}"
    assert depth == 0, f"Unbalanced parens, final depth={depth}"


# --- API endpoint checks ---
def test_api_root_alive():
    r = requests.get(f"{BASE_URL}/api/", timeout=30)
    assert r.status_code == 200


def test_source_archive_returns_valid_zip_with_ascii_bat():
    r = requests.get(f"{BASE_URL}/api/source/archive", timeout=120)
    assert r.status_code == 200
    assert "zip" in r.headers.get("content-type", "").lower()
    z = zipfile.ZipFile(io.BytesIO(r.content))
    names = z.namelist()
    # required entries
    for req in [BAT_NAME, "backend/pipeline.py", "backend/server.py", "frontend/src/App.js", "README.md"]:
        assert any(n.endswith(req) for n in names), f"Missing {req} in archive"
    bat_name = next(n for n in names if n.endswith(BAT_NAME))
    bat = z.read(bat_name)
    assert bat[:3] != b"\xef\xbb\xbf", "Zipped bat has BOM"
    assert not [b for b in bat if b > 127], "Zipped bat has non-ASCII bytes"
    assert b"Installing backend dependencies" in bat, "Zipped bat is not the new ASCII version"
