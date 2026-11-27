"""Offline provider-contract regressions; no paid API calls."""

import io
import json
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace as N
from unittest.mock import Mock, patch

from aibenchmark_esw import cli
from aibenchmark_esw.dataset import DatasetLoader
from aibenchmark_esw.errors import error_diagnostic
from aibenchmark_esw.llm import litellm_available
from aibenchmark_esw.llm.client import (
    ContextWindowError,
    LLMClient,
    is_fatal_provider_error,
)
from aibenchmark_esw.llm.costs import (
    CostBudget,
    PricingUnknownError,
    pricing,
    token_upper_bound,
    usage_cost,
)
from aibenchmark_esw.metrics.comparison import compare_runs
from aibenchmark_esw.metrics.reporter import BenchmarkReporter


def response(code):
    return N(model='fixture', usage=N(prompt_tokens=2, completion_tokens=3, total_tokens=5),
             choices=[N(finish_reason='stop', message=N(content=code))])


class TestProviderIssueFixes(unittest.TestCase):
    def invoke(self, arguments):
        with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            return cli.cmd_run(cli.build_parser().parse_args(arguments))

    def test_backoff_is_separate_and_reservations_settle_before_wait(self):
        provider = Mock()
        budget = CostBudget(.001)
        elapsed = [0.0]
        calls = []
        def complete(**kwargs):
            elapsed[0] += .25
            calls.append(kwargs)
            if len(calls) < 3:
                error = RuntimeError('limited')
                error.status_code = 429
                if len(calls) == 1:
                    error.response = N(headers={'Retry-After': '35'})
                raise error
            return response('int f(void){return 1;}')
        provider.completion.side_effect = complete
        def sleep(seconds):
            self.assertEqual(budget.snapshot()['reserved_usd'], 0)
            self.assertEqual(budget.snapshot()['charged_usd'], 0)
            elapsed[0] += seconds
        client = LLMClient('mock', max_tokens=10, max_retries=2, retry_backoff_seconds=10,
                           input_cost_per_million=1, output_cost_per_million=1, budget=budget)
        with patch.dict(sys.modules, {'litellm': provider}), \
                patch('aibenchmark_esw.llm.client.time.perf_counter', side_effect=lambda: elapsed[0]), \
                patch('aibenchmark_esw.llm.client.time.sleep', side_effect=sleep):
            client.generate_solution([{'role': 'user', 'content': 'task'}])
        self.assertEqual([a['latency_seconds'] for a in client.last_generation['attempts']], [.25] * 3)
        self.assertEqual(client.last_generation['latency_seconds'], .75)
        self.assertEqual(client.last_generation['elapsed_wall_seconds'], 55.75)
        self.assertEqual([a['retry_delay_source'] for a in client.last_generation['attempts'][:2]],
                         ['retry-after', 'exponential-backoff'])
        self.assertAlmostEqual(budget.spent, 5e-6)
        self.assertIsNone(client.last_generation['cost_usd'])  # Budget policy does not fabricate billing evidence.

    def test_retry_after_delay_forms_and_bounds(self):
        for status, header, where, delay, source in (
            (429, '30', 'response', 30, 'retry-after'),
            (429, '3.5', 'error', 3.5, 'retry-after'),
            (429, 'Thu, 01 Jan 1970 00:00:25 GMT', 'response', 15, 'retry-after'),
            (429, 'Thu, 01 Jan 1970 00:00:05 GMT', 'response', 2, 'exponential-backoff'),
            (429, '3600', 'response', 60, 'retry-after'),
            (429, '0', 'response', 2, 'exponential-backoff'),
            (429, '-1', 'response', 2, 'exponential-backoff'),
            (429, 'NaN', 'response', 2, 'exponential-backoff'),
            (429, 'Infinity', 'response', 2, 'exponential-backoff'),
            (429, '9' * 400, 'response', 2, 'exponential-backoff'),
            (429, 'not a date', 'response', 2, 'exponential-backoff'),
            (503, '30', 'response', 2, 'exponential-backoff'),
        ):
            with self.subTest(status=status, header=header, where=where):
                error = RuntimeError('provider error')
                error.status_code = status
                carrier = error if where == 'error' else N()
                carrier.headers = {'rEtRy-AfTeR': header}
                if where == 'response':
                    error.response = carrier
                provider = Mock(model_cost={})
                provider.completion.side_effect = [error, response('int f(void){return 1;}')]
                client = LLMClient('mock', max_retries=1, retry_backoff_seconds=2)
                with patch.dict(sys.modules, {'litellm': provider}), \
                        patch('aibenchmark_esw.llm.client.time.time', return_value=10), \
                        patch('aibenchmark_esw.llm.client.time.sleep') as wait:
                    client.generate_solution([{'role': 'user', 'content': 'task'}])
                wait.assert_called_once_with(delay)
                attempts = client.last_generation['attempts']
                self.assertEqual(len(attempts), 2)
                self.assertEqual(attempts[0]['retry_delay_seconds'], delay)
                self.assertEqual(attempts[0]['retry_delay_source'], source)
                self.assertNotIn('retry_delay_seconds', attempts[1])

    def test_retry_after_wait_can_be_cancelled(self):
        error = RuntimeError('limited')
        error.status_code = 429
        error.response = N(headers={'Retry-After': '30'})
        provider = Mock(model_cost={})
        provider.completion.side_effect = error
        client = LLMClient('mock', max_retries=1)
        event = Mock()
        event.is_set.return_value = False
        event.wait.return_value = True
        client.cancel_event = event
        with patch.dict(sys.modules, {'litellm': provider}), self.assertRaisesRegex(InterruptedError, 'cancelled'):
            client.generate_solution([{'role': 'user', 'content': 'task'}])
        event.wait.assert_called_once_with(30)
        self.assertEqual(len(client.last_generation['attempts']), 1)

    def test_ambiguous_timeout_retains_conservative_charge(self):
        provider = Mock()
        provider.completion.side_effect = TimeoutError('unknown server work')
        budget = CostBudget(1)
        client = LLMClient('mock', max_tokens=10, input_cost_per_million=1, output_cost_per_million=1, budget=budget)
        with patch.dict(sys.modules, {'litellm': provider}), self.assertRaises(TimeoutError):
            client.generate_solution([{'role': 'user', 'content': 'task'}])
        self.assertGreater(budget.spent, 0)
        self.assertEqual(budget.reserved, 0)

    def test_provider_secrets_are_scrubbed_in_saved_artifacts(self):
        provider = Mock()
        provider.completion.side_effect = RuntimeError('sk-super-secret Bearer bearer-secret api_key="query-secret" '
            'https://user:password@example.test/?access_token=token-secret custom-secret ' + 'x' * 4000)
        with TemporaryDirectory() as directory, patch.dict(sys.modules, {'litellm': provider}), \
                patch.dict('os.environ', {'BENCH_TEST_KEY': 'custom-secret'}):
            path = Path(directory) / 'failed.json'
            self.assertEqual(self.invoke(['run', '--model', 'mock', '--tasks', 'tier1_crc16',
                '--api-key-env', 'BENCH_TEST_KEY', '--output', str(path)]), 1)
            report = json.loads(path.read_text(encoding='utf-8'))
            results = BenchmarkReporter.from_json_dict(report)
            evidence = path.read_text(encoding='utf-8') + BenchmarkReporter.generate_markdown(results, 'mock')
            from aibenchmark_esw.metrics.exports import generate_sarif, report_html
            from aibenchmark_esw.metrics.junit import render_junit
            evidence += report_html(results, 'mock') + json.dumps(generate_sarif(results, 'mock')) + render_junit(results, 'mock')
            for secret in ('sk-super-secret', 'bearer-secret', 'query-secret', 'user:password', 'token-secret', 'custom-secret'):
                self.assertNotIn(secret, evidence)
            self.assertIn('RuntimeError', evidence)
            self.assertLess(len(results[0].error_log), 2100)
            self.assertEqual(report['metadata']['run_options']['api_key_env'], 'BENCH_TEST_KEY')
            self.assertEqual(provider.completion.call_args.kwargs['api_key'], 'custom-secret')
        self.assertIn('[REDACTED]', error_diagnostic(ValueError("{'api_key': 'hidden'}")))

    def test_interleaved_transient_stays_pending_and_resume_retries_only_it(self):
        loader = DatasetLoader()
        tasks = loader.list_tasks()[:3]
        provider = Mock()
        provider.completion.side_effect = [response(loader.get_reference_solution(tasks[0].id)),
            TimeoutError('temporary'), response(loader.get_reference_solution(tasks[2].id))]
        with TemporaryDirectory() as directory, patch.dict(sys.modules, {'litellm': provider}):
            path = Path(directory) / 'run.json'
            self.assertEqual(self.invoke(['run', '--model', 'mock', '--tasks', ','.join(t.id for t in tasks),
                                         '--output', str(path)]), 1)
            before = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(before['metadata']['pending_tasks'], [tasks[1].id])
            self.assertEqual(before['metadata']['run_status'], 'aborted')
            provider.completion.side_effect = [response(loader.get_reference_solution(tasks[1].id))]
            self.assertEqual(self.invoke(['run', '--resume', str(path)]), 0)
            after = json.loads(path.read_text(encoding='utf-8'))
            self.assertEqual(provider.completion.call_count, 4)
            self.assertEqual(after['tasks'][0], before['tasks'][0])
            self.assertEqual(after['tasks'][2], before['tasks'][2])
            self.assertEqual(after['metadata']['pending_tasks'], [])
            self.assertEqual(len(after['tasks'][1]['generation']['history']), 1)
            BenchmarkReporter.from_json_dict(after)

    def test_provider_price_fallbacks_and_cached_token_billing(self):
        provider = N(model_cost={'mock': {'input_cost_per_token': 2e-6, 'output_cost_per_token': 4e-6,
                                         'cache_read_input_token_cost': .5e-6}})
        prices = pricing(provider, 'openai/mock')
        self.assertAlmostEqual(usage_cost({'prompt_tokens': 100, 'completion_tokens': 10,
                                          'prompt_tokens_details': {'cached_tokens': 80}}, prices), .00012)
        self.assertEqual(prices['cache_pricing_source'], 'model-table')
        provider.model_cost['openai/mock'] = {'max_input_tokens': 100}
        self.assertEqual(pricing(provider, 'openai/mock'), prices)
        fallback = N(model_cost={}, get_model_info=lambda **kwargs: provider.model_cost['mock'])
        self.assertEqual(pricing(fallback, 'openai/mock'), prices)
        self.assertIsNone(usage_cost({'prompt_tokens': 1, 'completion_tokens': 2,
                                     'prompt_tokens_details': {'cached_tokens': 3}}, prices))
        with self.assertRaisesRegex(PricingUnknownError, '--input-cost-per-million'):
            CostBudget(1).reserve(None)
        self.assertFalse(is_fatal_provider_error(PricingUnknownError()))

    def test_dry_run_uses_provider_prices_and_the_identical_initial_prompt(self):
        provider = Mock()
        provider.model_cost = {'mock': {'input_cost_per_token': 1e-6, 'output_cost_per_token': 2e-6}}
        task = DatasetLoader().get_task('tier1_crc16')
        with TemporaryDirectory() as directory, patch.dict(sys.modules, {'litellm': provider}):
            path = Path(directory) / 'estimate.json'
            self.assertEqual(self.invoke(['run', '--model', 'openai/mock', '--tasks', task.id,
                                         '--dry-run', '--max-tokens', '10', '--output', str(path)]), 0)
            estimate = json.loads(path.read_text(encoding='utf-8'))
            messages = LLMClient('openai/mock').build_prompt(task.prompt, cli._task_headers(task),
                                                           DatasetLoader().get_starter_code(task.id))
            self.assertEqual(estimate['tasks'][0]['prompt_token_upper_bound'], token_upper_bound(messages))
            self.assertGreater(estimate['estimated_total_upper_usd'], 0)
            provider.completion.assert_not_called()

    def test_context_precheck_and_optional_turn_trim(self):
        provider = Mock()
        provider.model_cost = {'mock': {'max_input_tokens': 20, 'max_tokens': 30}}
        provider.token_counter.return_value = 40
        client = LLMClient('mock', max_tokens=10)
        with patch.dict(sys.modules, {'litellm': provider}), self.assertRaisesRegex(ContextWindowError, 'reduce'):
            client.generate_solution([{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'task'}])
        provider.completion.assert_not_called()
        provider.token_counter.side_effect = lambda **kw: 40 if len(kw['messages']) > 3 else 10
        provider.completion.side_effect = [response('Plan'), response('int f(void){return 1;}')]
        client = LLMClient('mock', max_tokens=10, prompt_strategy='plan')
        with patch.dict(sys.modules, {'litellm': provider}):
            client.generate_solution([{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'task'}])
        self.assertTrue(client.last_generation['context_trimmed'])
        self.assertEqual(len(client.last_generation['request_messages'][1]), 3)
        self.assertEqual([t['_turn_kind'] for t in client.last_generation['turns']], ['plan', 'implement'])

    def test_provider_context_error_retries_once_with_optional_turns_removed(self):
        error_type = type('ContextWindowExceededError', (Exception,), {})
        provider = Mock()
        provider.completion.side_effect = [response('Plan'), error_type('too large'), response('int f(void){return 1;}')]
        client = LLMClient('mock', prompt_strategy='plan')
        with patch.dict(sys.modules, {'litellm': provider}):
            client.generate_solution([{'role': 'system', 'content': 'rules'}, {'role': 'user', 'content': 'task'}])
        self.assertEqual(provider.completion.call_count, 3)
        self.assertTrue(client.last_generation['attempts'][1]['context_retry'])
        self.assertEqual(len(provider.completion.call_args.kwargs['messages']), 3)

    def test_compile_repair_is_opt_in_bounded_and_accounted(self):
        loader = DatasetLoader()
        provider = Mock()
        with TemporaryDirectory() as directory, patch.dict(sys.modules, {'litellm': provider}):
            path = Path(directory) / 'repair.json'
            provider.completion.side_effect = [response('this will not compile'), response(loader.get_reference_solution('tier1_crc16'))]
            self.assertEqual(self.invoke(['run', '--model', 'mock', '--tasks', 'tier1_crc16', '--repair-turns', '1',
                                         '--output', str(path), '--save-solutions', directory]), 0)
            report = json.loads(path.read_text(encoding='utf-8'))
            generation = report['tasks'][0]['generation']
            self.assertEqual([t['_turn_kind'] for t in generation['turns']], ['implement', 'repair'])
            self.assertEqual(generation['usage']['total_tokens'], 10)
            self.assertIn('compiler diagnostics', generation['request_messages'][1][-1]['content'])
            self.assertEqual((Path(directory) / 'tier1_crc16.c').read_text(encoding='utf-8'), loader.get_reference_solution('tier1_crc16').strip())
            BenchmarkReporter.from_json_dict(report)
            with self.assertRaisesRegex(ValueError, 'repair-turns'):
                self.invoke(['run', '--resume', str(path), '--repair-turns', '0'])
            other = json.loads(path.read_text(encoding='utf-8'))
            other['model_name'] = 'other'
            other['tasks'][0]['model_name'] = 'other'
            other['metadata']['generation_settings']['repair_turns'] = 0
            with self.assertRaisesRegex(ValueError, 'Incompatible'):
                compare_runs([report, other])
            provider.completion.side_effect = [response('still invalid'), response('still invalid')]
            self.assertEqual(self.invoke(['run', '--model', 'mock', '--tasks', 'tier1_crc16', '--repair-turns', '1',
                                         '--output', str(path)]), 1)
            self.assertEqual(len(json.loads(path.read_text(encoding='utf-8'))['tasks'][0]['generation']['turns']), 2)
            previous_calls = provider.completion.call_count
            provider.completion.side_effect = [response(loader.get_starter_code('tier1_crc16'))]
            self.assertEqual(self.invoke(['run', '--model', 'mock', '--tasks', 'tier1_crc16', '--repair-turns', '1',
                                         '--output', str(path)]), 1)
            self.assertEqual(provider.completion.call_count - previous_calls, 1)
            self.assertTrue(json.loads(path.read_text(encoding='utf-8'))['tasks'][0]['compiled'])

    def test_version_missing_command_and_optional_provider_detection(self):
        from aibenchmark_esw import __version__
        with redirect_stdout(io.StringIO()) as output, self.assertRaises(SystemExit) as status:
            cli.build_parser().parse_args(['--version'])
        self.assertEqual(status.exception.code, 0)
        self.assertIn(__version__, output.getvalue())
        with patch.object(sys, 'argv', ['aibenchmark-esw']), redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as status:
            cli.main()
        self.assertEqual(status.exception.code, 2)
        with patch.dict(sys.modules, {'litellm': None}):
            self.assertFalse(litellm_available())
        with patch.dict(sys.modules, {'litellm': Mock()}):
            self.assertTrue(litellm_available())


if __name__ == '__main__':
    unittest.main()
