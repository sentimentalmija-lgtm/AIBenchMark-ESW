import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from xml.etree import ElementTree as ET


def runner():
    path=Path('.github/scripts/action_runner.py')
    spec=importlib.util.spec_from_file_location('action_runner',path)
    module=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ConsumerActionTests(unittest.TestCase):
    def invoke(self, root, name, **inputs):
        outputs=root/f'{name}-outputs.txt'
        summary=root/f'{name}-summary.md'
        environment=dict(os.environ,INPUT_COMMAND='run',INPUT_MODEL='baseline',INPUT_TASKS='tier1_crc16',
                         INPUT_OUTPUT_DIRECTORY=str(root/'artifacts'),GITHUB_OUTPUT=str(outputs),
                         GITHUB_STEP_SUMMARY=str(summary),AIBENCHMARK_ESW_CPPCHECK='off')
        environment.update(inputs)
        process=subprocess.run([sys.executable,'.github/scripts/action_runner.py','benchmark'],
                               env=environment,capture_output=True,text=True,timeout=60)
        values=dict(line.split('=',1) for line in outputs.read_text(encoding='utf-8').splitlines())
        return process,values,summary

    def test_default_run_is_offline_baseline_with_json_junit(self):
        command,outputs=runner().build_command({})
        self.assertIn('run',command)
        self.assertEqual(command[command.index('--model')+1],'baseline')
        self.assertEqual(command[command.index('--tasks')+1],'tier1_crc16')
        self.assertIn('--junit-output',command)
        self.assertEqual(set(outputs),{'json','junit'})

    def test_eval_solution_arguments_are_separate_tokens(self):
        command,_=runner().build_command({'INPUT_COMMAND':'eval','INPUT_SOLUTION':'my source;echo bad.c','INPUT_TASKS':'tier1_crc16','INPUT_EXTRA_ARGUMENTS':'["--compile-timeout", "12"]'})
        self.assertEqual(command[command.index('--solution')+1],'my source;echo bad.c')
        self.assertNotIn('--reference',command)
        self.assertIn('--compile-timeout',command)

    def test_invalid_versions_commands_and_output_overrides_rejected(self):
        module=runner()
        for value in ('a>=1','--index-url secret',''):
            if value:
                with self.assertRaises(ValueError):
                    module.install_spec(value,'run')
        for env in ({'INPUT_COMMAND':'shell'},{'INPUT_EXTRA_ARGUMENTS':'["--output=source.c"]'},{'INPUT_EXTRA_ARGUMENTS':'["--junit-output", "source.c"]'},{'INPUT_EXTRA_ARGUMENTS':'"--reference"'}):
            with self.assertRaises(ValueError):
                module.build_command(env)

    def test_real_offline_baseline_helper_writes_artifacts_and_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            process,outputs,_=self.invoke(root,'baseline')
            self.assertEqual(process.returncode,0,process.stderr)
            report=json.loads(Path(outputs['json']).read_text(encoding='utf-8'))
            self.assertEqual(report['model_name'],'baseline')
            self.assertEqual(report['pass_at_1_pct'],100)
            self.assertTrue(Path(outputs['junit']).is_file())
            self.assertEqual(Path(outputs['json']).parent.parent,(root/'artifacts').resolve())
            self.assertEqual(outputs['exit-code'],'0')

    def test_failed_new_invocation_cannot_publish_previous_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            first,old_outputs,first_summary=self.invoke(root,'first')
            self.assertEqual(first.returncode,0,first.stderr)
            old_artifacts={key:Path(old_outputs[key]).read_bytes() for key in ('json','junit')}
            self.assertIn('100.00',first_summary.read_text(encoding='utf-8'))
            failed,new_outputs,failed_summary=self.invoke(root,'failed',INPUT_TASKS='no_such_task')
            self.assertEqual(failed.returncode,1,failed.stderr)
            self.assertEqual(new_outputs['exit-code'],'1')
            self.assertEqual(new_outputs['raw-exit-code'],'1')
            for key,content in old_artifacts.items():
                self.assertNotEqual(new_outputs[key],old_outputs[key])
                self.assertFalse(Path(new_outputs[key]).exists())
                self.assertEqual(Path(old_outputs[key]).read_bytes(),content)
            self.assertFalse(failed_summary.exists())
            self.assertIn('Benchmark summary unavailable',failed.stderr)

    def test_new_failure_checkpoint_is_exported_without_old_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            first,old_outputs,_=self.invoke(root,'first')
            self.assertEqual(first.returncode,0,first.stderr)
            candidate=root/'broken.c'
            candidate.write_text('this is not valid C;\n',encoding='utf-8')
            failed,new_outputs,summary=self.invoke(root,'failed',INPUT_COMMAND='eval',INPUT_SOLUTION=str(candidate))
            self.assertEqual(failed.returncode,1,failed.stderr)
            self.assertEqual(new_outputs['exit-code'],'1')
            for key in ('json','junit'):
                self.assertNotEqual(new_outputs[key],old_outputs[key])
                self.assertTrue(Path(new_outputs[key]).is_file())
                self.assertTrue(Path(old_outputs[key]).is_file())
            report=json.loads(Path(new_outputs['json']).read_text(encoding='utf-8'))
            self.assertFalse(report['tasks'][0]['compiled'])
            self.assertEqual(report['overall_score'],0)
            self.assertEqual(ET.parse(new_outputs['junit']).getroot().get('errors'),'1')
            self.assertIn('Generation or compilation did not succeed',Path(new_outputs['junit']).read_text(encoding='utf-8'))
            self.assertNotIn('100.00',summary.read_text(encoding='utf-8'))

    def test_action_uploads_even_when_benchmark_fails_and_smoke_is_offline(self):
        action=Path('action.yml').read_text()
        self.assertIn('using: composite',action)
        self.assertIn('if: always()',action)
        self.assertIn('actions/upload-artifact@',action)
        self.assertIn('continue-on-error: true',action)
        workflow=Path('.github/workflows/action-smoke.yml').read_text()
        self.assertIn('uses: ./',workflow)
        self.assertIn('model: baseline',workflow)
