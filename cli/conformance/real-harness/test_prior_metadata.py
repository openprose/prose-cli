#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest


HERE = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location(
    "openprose_prior_metadata", HERE / "prior_metadata.py"
)
assert SPEC is not None and SPEC.loader is not None
metadata = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = metadata
SPEC.loader.exec_module(metadata)


class PriorMetadataTest(unittest.TestCase):
    def test_extract_emits_only_bounded_metadata_and_model_ids(self) -> None:
        with tempfile.TemporaryDirectory() as raw:
            source = Path(raw) / "private.jsonl"
            source.write_bytes(
                b"secret prompt openai/gpt-5.4 api_key=do-not-copy\n"
                + b"openrouter/deepseek/deepseek-v4-flash more private text\n"
                + b"x" * 100
            )
            result = metadata.extract(source, maximum_bytes=140, maximum_ids=10)
        serialized = repr(result)
        self.assertEqual("private.jsonl", result["sourceBasename"])
        self.assertLessEqual(result["bytesScanned"], 140)
        self.assertTrue(result["scanTruncated"])
        self.assertEqual(
            ["openai/gpt-5.4", "openrouter/deepseek/deepseek-v4-flash"],
            result["modelIds"],
        )
        self.assertNotIn("secret prompt", serialized)
        self.assertNotIn("do-not-copy", serialized)


if __name__ == "__main__":
    unittest.main(verbosity=2)
