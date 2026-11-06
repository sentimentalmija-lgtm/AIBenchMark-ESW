import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from aibenchmark_esw.cli import _target_task
from aibenchmark_esw.dataset import DatasetLoader
from aibenchmark_esw.models import TaskConfig


class TestTargetLimitSelectors(unittest.TestCase):
    def test_invalid_selectors_are_rejected_by_strict_and_diagnostic_loading(self):
        for selector in ('arm:cortex-mO', 'arm:banana', 'arm:', 'cortex-m2', 'rv32banana', ''):
            with self.subTest(selector=selector), TemporaryDirectory() as directory:
                root = Path(directory)
                task = root / 'external'
                task.mkdir()
                data = {'id': 'external', 'limits': {'targets': {selector: {'max_flash_bytes': 1}}}}
                (task / 'task.json').write_text(json.dumps(data), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'limits.targets'):
                    DatasetLoader(root)
                loader = DatasetLoader(root, strict=False)
                self.assertEqual(loader.list_tasks(), [])
                self.assertIn('limits.targets', loader.load_errors[str(task)])

    def test_valid_full_and_shorthand_selectors_apply_without_a_compiler(self):
        for target in ('arm:cortex-m0', 'arm:cortex-m0plus', 'arm:cortex-m85',
                       'avr:atmega328p', 'riscv:rv32imac', 'riscv:rv64gc'):
            for selector in (target, target.split(':')[1]):
                with self.subTest(selector=selector):
                    task = TaskConfig.from_dict({'id': 'external', 'limits': {
                        'targets': {selector: {'max_flash_bytes': 1}}}}, Path('external'))
                    self.assertEqual(_target_task(task, SimpleNamespace(target=target)).limits.max_flash_bytes, 1)
                    self.assertEqual(_target_task(task, SimpleNamespace(target=None)).limits.max_flash_bytes, 2048)
