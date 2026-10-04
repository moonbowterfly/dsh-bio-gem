"""Local gapseq readiness must not depend on Zenodo network availability."""
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
import gapseq_wsl  # noqa: E402


class GapseqProbeTests(unittest.TestCase):
    def responses(self, counts, metadata='{"zenodoID":[20446806],"version":["1.5"]}'):
        return [
            (0, "Linux test", ""),
            (0, "ID=ubuntu", ""),
            (0, "gapseq version: 2.1.0", ""),
            (0, metadata + "\n" + "\n".join(map(str, counts)), ""),
        ]

    def test_local_database_is_usable_without_online_version_check(self):
        with patch.object(gapseq_wsl, "wsl_run", side_effect=self.responses([9887, 19117, 9887])) as run:
            result = gapseq_wsl.probe()
        self.assertTrue(result["capable"])
        self.assertEqual(result["seqdb_version"], "1.5")
        self.assertEqual(result["seqdb_counts"]["rxn"], 19117)
        self.assertIn("version_seqDB.json", run.call_args_list[-1].args[0])
        self.assertNotIn("update-sequences", run.call_args_list[-1].args[0])

    def test_missing_reaction_database_fails_closed(self):
        with patch.object(gapseq_wsl, "wsl_run", side_effect=self.responses([9887, 0, 9887])):
            result = gapseq_wsl.probe()
        self.assertFalse(result["capable"])
        self.assertFalse(result["checks"]["seqdb"])

    def test_malformed_metadata_fails_closed(self):
        with patch.object(gapseq_wsl, "wsl_run", side_effect=self.responses([9887, 19117, 9887], '[]')):
            result = gapseq_wsl.probe()
        self.assertEqual(result["level"], "DEGRADED")
        self.assertFalse(result["checks"]["seqdb"])


if __name__ == "__main__":
    unittest.main()
