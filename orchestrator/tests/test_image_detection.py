from pathlib import Path
from factory.api import _detect_image_from_path


def test_detect_php(tmp_path):
    (tmp_path / "composer.json").write_text("{}")
    assert _detect_image_from_path(tmp_path) == "factory-agent:php"


def test_detect_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]")
    assert _detect_image_from_path(tmp_path) == "factory-agent:python"


def test_detect_node(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert _detect_image_from_path(tmp_path) == "factory-agent:node"


def test_detect_fallback(tmp_path):
    assert _detect_image_from_path(tmp_path) == "factory-agent:base"


def test_detect_php_priority_over_node(tmp_path):
    (tmp_path / "composer.json").write_text("{}")
    (tmp_path / "package.json").write_text("{}")
    assert _detect_image_from_path(tmp_path) == "factory-agent:php"
