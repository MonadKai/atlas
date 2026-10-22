# SPDX-License-Identifier: AGPL-3.0-only
"""Explicit optional toolkit evolution preserves older closed releases."""
import sys
from pathlib import Path
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "deploy/release"))
from release_contract import validate_tool_sets, RELIABILITY_MEMBERS, RELIABILITY_MEMBERS_V1

class ToolSetTests(unittest.TestCase):
    def test_old_bundle_without_extension_remains_supported(self):
        validate_tool_sets({}, {"toolkit.py"})

    def test_complete_declared_closure_is_required(self):
        for version, members in (("atlas-reliability-tools-v1", RELIABILITY_MEMBERS_V1),
                                 ("atlas-reliability-tools-v2", RELIABILITY_MEMBERS)):
            release = {"tool_sets": [version]}
            validate_tool_sets(release, set(members))
            for missing in members:
                with self.subTest(version=version, missing=missing), self.assertRaises(ValueError):
                    validate_tool_sets(release, set(members) - {missing})

    def test_v1_does_not_retroactively_require_v2_normalizer(self):
        self.assertNotIn("experiments/resources.py", RELIABILITY_MEMBERS_V1)
        validate_tool_sets({"tool_sets": ["atlas-reliability-tools-v1"]}, RELIABILITY_MEMBERS_V1)
        with self.assertRaises(ValueError):
            validate_tool_sets({"tool_sets": ["atlas-reliability-tools-v2"]}, RELIABILITY_MEMBERS_V1)

    def test_unknown_duplicate_or_malformed_tool_sets_fail_closed(self):
        for declaration in (None, "atlas-reliability-tools-v1", [], ["unknown"],
                            ["atlas-reliability-tools-v1"] * 2):
            with self.subTest(declaration=declaration), self.assertRaises(ValueError):
                validate_tool_sets({"tool_sets": declaration}, set(RELIABILITY_MEMBERS))

if __name__ == "__main__":
    unittest.main()
