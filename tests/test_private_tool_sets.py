# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit optional toolkit evolution preserves older closed releases."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/release"))
from release_contract import validate_tool_sets, RELIABILITY_MEMBERS

class ToolSetTests(unittest.TestCase):
    def test_old_bundle_without_extension_remains_supported(self):
        validate_tool_sets({}, {"toolkit.py"})

    def test_complete_declared_closure_is_required(self):
        release = {"tool_sets": ["atlas-reliability-tools-v1"]}
        validate_tool_sets(release, set(RELIABILITY_MEMBERS))
        for missing in RELIABILITY_MEMBERS:
            with self.subTest(missing=missing), self.assertRaises(ValueError):
                validate_tool_sets(release, set(RELIABILITY_MEMBERS) - {missing})

    def test_unknown_duplicate_or_malformed_tool_sets_fail_closed(self):
        for declaration in (None, "atlas-reliability-tools-v1", [], ["unknown"],
                            ["atlas-reliability-tools-v1"] * 2):
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                validate_tool_sets({"tool_sets": declaration}, set(RELIABILITY_MEMBERS))

if __name__ == "__main__":
    unittest.main()
