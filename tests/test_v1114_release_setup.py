from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_release_readme_names_are_chinese_default_and_english_secondary():
    assert (ROOT / "README.md").exists()
    assert (ROOT / "README_EN.md").exists()
    assert not (ROOT / "README_ZH.md").exists()

    zh = (ROOT / "README.md").read_text(encoding="utf-8")
    en = (ROOT / "README_EN.md").read_text(encoding="utf-8")
    assert "English README" in zh
    assert "README_EN.md" in zh
    assert "中文" in en
    assert "README.md" in en


def test_release_hygiene_checks_new_readme_names():
    script = (ROOT / "scripts" / "check-release.py").read_text(encoding="utf-8")
    assert 'ROOT / "README.md", ROOT / "README_EN.md"' in script
    assert '".gitignore", "README.md", "README_EN.md", "CHANGELOG.md", "AGENTS.example.md"' in script
    assert 'if (ROOT / "README_ZH.md").exists()' in script


def test_setup_rag_avoids_native_stdin_bom_and_fails_fast():
    script = (ROOT / "scripts" / "setup-rag.ps1").read_text(encoding="utf-8")
    # Windows PowerShell can inject U+FEFF when piping a here-string to native stdin.
    assert "| & $Python -" not in script
    assert "& $Python -c $CleanCode" in script
    assert "TrimStart([char]0xFEFF)" in script
    # Native process failures must not fall through to WORKBOT_RAG_SETUP_OK.
    assert script.count("$LASTEXITCODE -ne 0") >= 2
    assert 'throw "$Name failed with exit code $LASTEXITCODE"' in script
    assert 'Write-Host "WORKBOT_RAG_SETUP_OK"' in script
