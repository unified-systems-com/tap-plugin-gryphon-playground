"""Plugin structure / manifest validation for the gryphon_playground plugin.

Structure-level validation runs without Django — it confirms the manifest,
edge files, model dotted paths, and directory layout are well-formed.
"""

from pathlib import Path

from tap_plugins.validate.service import validate_plugin

# The plugin PROJECT dir (the one holding pyproject.toml + the package), which is what
# validate_plugin takes — not the package dir. Now that the tests live inside the package
# (tap_plugin/gryphon_playground/tests/), that is three levels up, not one.
PLUGIN_ROOT = Path(__file__).resolve().parents[3]


class TestStructure:
    def test_structure_passes(self):
        result = validate_plugin(PLUGIN_ROOT, level="structure")
        assert result.ok, result.to_human()

    def test_strict_passes(self):
        result = validate_plugin(PLUGIN_ROOT, level="structure", strict=True)
        assert result.ok, result.to_human()
