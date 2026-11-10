import subprocess
import unittest
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from compiler_tools import find_clang

from aibenchmark_esw.dataset import DatasetLoader
from aibenchmark_esw.sandbox.executor import ExecutionSandbox


class TestClangClStandard(unittest.TestCase):
    def test_driver_specific_standard_and_object_flags(self):
        task = DatasetLoader().get_task('tier1_crc16')
        for standard in ('c99', 'c11', 'c17'):
            with self.subTest(standard=standard), TemporaryDirectory() as directory:
                executor = ExecutionSandbox(compiler_path='clang-cl')
                with patch('aibenchmark_esw.sandbox.executor.run_bounded',
                           return_value=subprocess.CompletedProcess([], 0, b'')) as runner:
                    result = executor.compile_object(replace(task, target_standard=standard),
                                                     'int f(void){return 0;}', Path(directory))
                command = runner.call_args.args[0]
                self.assertIn('/clang:-std=' + standard, command)
                self.assertIn('/c', command)
                self.assertEqual(result.effective_standard, standard)

    def test_real_compiler_standard_macro_matches_recorded_evidence(self):
        compiler = find_clang()
        if not compiler or not Path(compiler).with_name('clang-cl.exe').is_file():
            self.skipTest('Windows clang-cl unavailable')
        task = DatasetLoader().get_task('tier1_crc16')
        for standard, version in (('c99', 199901), ('c11', 201112), ('c17', 201710)):
            with self.subTest(standard=standard), TemporaryDirectory() as directory:
                code = f'#if __STDC_VERSION__ != {version}L\n#error Wrong standard\n#endif\nint f(void){{return 0;}}'
                executor = ExecutionSandbox(compiler_path=str(Path(compiler).with_name('clang-cl.exe')))
                result = executor.compile_object(replace(task, target_standard=standard), code, Path(directory))
                self.assertTrue(result.success, result.output)
                self.assertEqual(result.effective_standard, standard)
