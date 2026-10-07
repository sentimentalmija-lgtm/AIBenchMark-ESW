import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from aibenchmark_esw.sandbox.cross_compiler import CrossCompiler


class TestCrossCompiler(unittest.TestCase):
    def test_settings_preserve_resolved_path_stamp_and_selected_name(self):
        with TemporaryDirectory() as directory:
            executable = Path(directory) / "arm-none-eabi-gcc"
            executable.write_bytes(b"compiler fixture")
            with patch("aibenchmark_esw.sandbox.cross_compiler.subprocess.run",
                       return_value=SimpleNamespace(returncode=0, stdout="arm-none-eabi-gcc 13.2.1\n", stderr="")):
                settings = CrossCompiler("arm:cortex-m0", str(executable)).settings()

        self.assertEqual(settings["compiler"], str(executable.resolve()))
        self.assertEqual(settings["compiler_name"], "arm-none-eabi-gcc")
        self.assertEqual(settings["version"], "arm-none-eabi-gcc 13.2.1")
        self.assertEqual(settings["stamp"][0], len(b"compiler fixture"))
        self.assertEqual(settings["measurement"], "target-object")


if __name__ == "__main__":
    unittest.main()
