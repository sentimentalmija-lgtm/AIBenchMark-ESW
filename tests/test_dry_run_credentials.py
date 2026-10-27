import io
import json
import os
import unittest
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

from aibenchmark_esw.cli import _new_client, build_parser, cmd_run


class TestDryRunCredentials(unittest.TestCase):
    def test_offline_estimate_does_not_require_or_read_configured_credentials(self):
        for key in (None, '', 'FAKE_AUDIT_SECRET'):
            with self.subTest(key=key), patch.dict(os.environ, {}, clear=True):
                if key is not None:
                    os.environ['AUDIT_PROVIDER_KEY'] = key
                provider = Mock(model_cost={})
                args = build_parser().parse_args([
                    'run', '--model', 'mock', '--tasks', 'tier1_crc16', '--dry-run',
                    '--api-key-env', 'AUDIT_PROVIDER_KEY', '--max-tokens', '10',
                    '--input-cost-per-million', '1', '--output-cost-per-million', '1'])
                output = io.StringIO()
                environment_get = os.environ.get
                def read_environment(name, default=None, environment_get=environment_get):
                    if name == 'AUDIT_PROVIDER_KEY':
                        raise AssertionError('credentials read')
                    return environment_get(name, default)
                with patch.dict('sys.modules', {'litellm': provider}), redirect_stdout(output), \
                        patch('aibenchmark_esw.cli.os.environ.get', side_effect=read_environment):
                    self.assertEqual(cmd_run(args), 0)
                report = json.loads(output.getvalue())
                self.assertEqual(report['mode'], 'dry-run')
                self.assertIsNotNone(report['estimated_total_upper_usd'])
                self.assertNotIn('FAKE_AUDIT_SECRET', output.getvalue())
                provider.completion.assert_not_called()

    def test_live_client_still_requires_explicitly_configured_key(self):
        args = build_parser().parse_args(['run', '--model', 'mock', '--api-key-env', 'AUDIT_PROVIDER_KEY'])
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(ValueError, 'AUDIT_PROVIDER_KEY'):
            _new_client(args)
