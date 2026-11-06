import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from compiler_tools import find_clang

from aibenchmark_esw.dataset import DatasetLoader
from aibenchmark_esw.sandbox.cross_compiler import CrossCompiler


class TestCrossCompilerCase(unittest.TestCase):
    def test_clang_target_flags_are_independent_of_executable_case(self):
        cases = (
            ('arm:cortex-m0', ['--target=arm-none-eabi', '-mcpu=cortex-m0', '-mthumb']),
            ('avr:atmega328p', ['--target=avr', '-mmcu=atmega328p']),
            ('riscv:rv32imac', ['--target=riscv32-unknown-elf', '-march=rv32imac', '-mabi=ilp32']),
            ('riscv:rv64gc', ['--target=riscv64-unknown-elf', '-march=rv64gc', '-mabi=lp64d']),
        )
        for target, flags in cases:
            for executable in ('clang', 'clang.exe', 'CLANG.EXE', 'ClAnG.exe'):
                with self.subTest(target=target, executable=executable), patch(
                        'aibenchmark_esw.sandbox.cross_compiler.run_bounded',
                        return_value=subprocess.CompletedProcess([], 0, b'clang fixture', b'')):
                    cross = CrossCompiler(target, executable)
                    self.assertEqual(cross.flags, flags + ['-Os', '-ffreestanding'])
                    self.assertEqual(cross.settings()['flags'], cross.flags)

    def test_cross_gcc_keeps_driver_flags_for_both_cases(self):
        cases = (
            ('arm:cortex-m0', 'arm-none-eabi-gcc', ['-mcpu=cortex-m0', '-mthumb']),
            ('avr:atmega328p', 'avr-gcc', ['-mmcu=atmega328p']),
            ('riscv:rv64gc', 'riscv64-unknown-elf-gcc', ['-march=rv64gc', '-mabi=lp64d']),
        )
        for target, executable, flags in cases:
            for name in (executable, executable.upper() + '.EXE'):
                with self.subTest(target=target, executable=name), patch(
                        'aibenchmark_esw.sandbox.cross_compiler.run_bounded',
                        return_value=subprocess.CompletedProcess([], 0, b'gcc fixture', b'')):
                    cross = CrossCompiler(target, name)
                    self.assertEqual(cross.flags, flags + ['-Os', '-ffreestanding'])

    def test_rv64_rejects_riscv32_toolchain_in_any_case(self):
        for executable in ('riscv32-unknown-elf-gcc', 'RISCV32-UNKNOWN-ELF-GCC.EXE'):
            with self.subTest(executable=executable), patch(
                    'aibenchmark_esw.sandbox.cross_compiler.run_bounded',
                    return_value=subprocess.CompletedProcess([], 0, b'gcc fixture', b'')):
                with self.assertRaisesRegex(ValueError, 'RV64.*riscv32'):
                    CrossCompiler('riscv:rv64gc', executable)

    @unittest.skipUnless(os.name == 'nt', 'Alternate-case executable paths require Windows')
    def test_real_windows_clang_paths_produce_equivalent_target_objects(self):
        compiler = find_clang()
        if compiler is None:
            self.skipTest('Clang unavailable')
        compiler = Path(compiler)
        upper = compiler.with_name(compiler.name.upper())
        self.assertTrue(compiler.samefile(upper))
        backends = subprocess.run([str(compiler), '--print-targets'], capture_output=True,
                                  text=True, check=True, timeout=10).stdout
        task = DatasetLoader().get_task('tier1_crc16')
        with TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'probe.c'
            source.write_text('int probe(void) { return 1; }\n', encoding='utf-8')
            for target, backend in (('arm:cortex-m0', 'arm'), ('riscv:rv32imac', 'riscv32')):
                with self.subTest(target=target):
                    if backend not in backends:
                        self.skipTest(f'Installed Clang was built without the {backend} backend')
                    compilers = [CrossCompiler(target, str(path)) for path in (compiler, upper)]
                    self.assertEqual(compilers[0].flags, compilers[1].flags)
                    objects = []
                    for index, cross in enumerate(compilers):
                        output = root / f'probe{index}.o'
                        result = cross.compile(task, source, output)
                        self.assertTrue(result.success, result.output)
                        self.assertEqual(result.effective_standard, 'c99')
                        objects.append(output.read_bytes())
                    self.assertEqual(objects[0], objects[1])
