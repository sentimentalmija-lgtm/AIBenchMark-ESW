"""Run composite-action inputs as argument arrays, never as shell source."""

import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


def install_spec(version, mode, action_path=None, model='baseline'):
    if mode not in ('run','eval'):
        raise ValueError('command must be run or eval')
    extra='[llm]' if mode=='run' and model!='baseline' else ''
    if version:
        if not re.fullmatch(r'[0-9][A-Za-z0-9.!+_-]*',version):
            raise ValueError('package-version must be one exact package version')
        return 'aibenchmark-esw'+extra+'=='+version
    return str(Path(action_path or Path(__file__).resolve().parents[2]).resolve())+extra


def build_command(environment, *, isolate_outputs=False):
    mode=environment.get('INPUT_COMMAND') or 'run'
    if mode not in ('run','eval'):
        raise ValueError('command must be run or eval')
    directory=Path(environment.get('INPUT_OUTPUT_DIRECTORY') or '.aibenchmark-artifacts').resolve()
    command=[sys.executable,'-m','aibenchmark_esw.cli',mode]
    tasks=environment.get('INPUT_TASKS') or 'tier1_crc16'
    if mode=='run':
        command += ['--model',environment.get('INPUT_MODEL') or 'baseline','--tasks',tasks,'--jobs',environment.get('INPUT_JOBS') or '1']
    else:
        if ',' in tasks:
            raise ValueError('eval accepts exactly one task')
        command += ['--task',tasks]
        solution=environment.get('INPUT_SOLUTION')
        if solution:
            command += ['--solution',solution]
        else:
            command += ['--reference']
    compiler=environment.get('INPUT_COMPILER')
    if compiler:
        command += ['--compiler',compiler]
    extra=json.loads(environment.get('INPUT_EXTRA_ARGUMENTS') or '[]')
    if not isinstance(extra,list) or any(not isinstance(item,str) or '\x00' in item for item in extra):
        raise ValueError('extra-arguments must be a JSON array of argument strings')
    identity = {'model', 'tasks', 'task', 'tier', 'category', 'tasks_root', 'solution', 'reference',
                'output', 'junit_output', 'resume', 'save_solutions', 'compiler', 'jobs', 'dry_run',
                'help', 'version'}
    # Read aliases from this action revision, including flags added later.
    import argparse
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    try:
        from aibenchmark_esw.cli import build_parser
        parser = build_parser()
        subcommands = next(action for action in parser._actions if isinstance(action, argparse._SubParsersAction))
        actions = [*parser._actions, *subcommands.choices['run']._actions, *subcommands.choices['eval']._actions]
        reserved = {flag for action in actions
                    if action.dest in identity for flag in action.option_strings}
    finally:
        sys.path.pop(0)
    if any(item.split('=',1)[0] in reserved for item in extra):
        raise ValueError('extra-arguments cannot override dedicated identity, artifact, or control inputs')
    if isolate_outputs:
        directory.mkdir(parents=True,exist_ok=True)
        directory=Path(tempfile.mkdtemp(prefix='run-',dir=directory))
    outputs={'json':str(directory/'benchmark.json'),'junit':str(directory/'benchmark.junit.xml')}
    command += extra + ['--output',outputs['json'],'--junit-output',outputs['junit']]
    return command,outputs


def _outputs(values):
    path=os.environ.get('GITHUB_OUTPUT')
    if path:
        with open(path,'a',encoding='utf-8') as stream:
            for key,value in values.items():
                if '\n' in str(value) or '\r' in str(value):
                    raise ValueError('Action output values must fit one line')
                stream.write(f'{key}={value}\n')


def main():
    if len(sys.argv)!=2 or sys.argv[1] not in ('install','benchmark','check'):
        raise ValueError('Choose install, benchmark, or check')
    operation=sys.argv[1]
    if operation=='install':
        build_command(os.environ)  # Validate identity before selecting optional provider dependencies.
        spec=install_spec(os.environ.get('INPUT_PACKAGE_VERSION',''),os.environ.get('INPUT_COMMAND') or 'run',
                          os.environ.get('GITHUB_ACTION_PATH'),os.environ.get('INPUT_MODEL') or 'baseline')
        return subprocess.run([sys.executable,'-m','pip','install',spec]).returncode
    if operation=='check':
        return int(os.environ.get('BENCHMARK_EXIT_CODE') or '1')
    command,outputs=build_command(os.environ,isolate_outputs=True)
    _outputs(outputs)
    raw_code=subprocess.run(command).returncode
    code=128-raw_code if raw_code<0 else raw_code
    _outputs({'exit-code':code,'raw-exit-code':raw_code})
    summary=os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        from aibenchmark_esw.metrics.github_summary import github_summary
        try:
            report=json.loads(Path(outputs['json']).read_text(encoding='utf-8'))
            with open(summary,'a',encoding='utf-8') as stream:
                stream.write(github_summary(report)+'\n')
        except (OSError,ValueError,TypeError,KeyError) as error:
            print(f'Benchmark summary unavailable: {error}',file=sys.stderr)
    return code


if __name__=='__main__':
    try:
        sys.exit(main())
    except (OSError,ValueError) as error:
        print(f'Benchmark action input error: {error}',file=sys.stderr)
        sys.exit(1)
