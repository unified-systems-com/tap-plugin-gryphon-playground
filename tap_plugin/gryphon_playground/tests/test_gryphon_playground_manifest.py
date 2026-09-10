"""Plugin structure / manifest validation for the gryphon_playground plugin.

Structure-level validation runs without Django — it confirms the manifest,
edge files, model dotted paths, and directory layout are well-formed.
"""

import pytest

from tap.plugin_testing import find_plugin_source_root
from tap_plugins.validate.service import validate_plugin

# The plugin PROJECT dir (the one holding pyproject.toml + the package), which is what
# validate_plugin takes — not the package dir. Derived, never counted: a fixed
# `parents[3]` walk lands in site-packages under a wheel install and validates the
# install tree as if it were a source tree (tap#369 caught 20 such tests across six
# plugins). `find_plugin_source_root` returns None when there is no source tree.
PLUGIN_ROOT = find_plugin_source_root(__file__)

pytestmark = pytest.mark.skipif(
    PLUGIN_ROOT is None,
    reason="source-layout validation needs the plugin source tree; installed as a wheel here (delegated to the plugin repo's own build).",
)


class TestStructure:
    def test_structure_passes(self):
        result = validate_plugin(PLUGIN_ROOT, level="structure")
        assert result.ok, result.to_human()

    def test_strict_passes(self):
        result = validate_plugin(PLUGIN_ROOT, level="structure", strict=True)
        assert result.ok, result.to_human()
