import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from cgm.agent import Runner, parse_antigravity_events
from cgm.config import load
from cgm.store import Store
from cgm.toolapi import RunCtx, ToolAPI


def events(*items):
    return '\n'.join(json.dumps(item) for item in items).encode()


class AntigravityConfigTests(unittest.TestCase):
    def test_account_backend_needs_no_api_key_and_uses_cli_model(self):
        with patch.dict(os.environ, {'CGM_BACKEND': 'antigravity'}, clear=True):
            cfg = load(require_token=False)
        self.assertEqual(cfg.backend, 'antigravity')
        self.assertEqual(cfg.model, '')
        self.assertEqual(cfg.image_model, '')
        self.assertEqual(cfg.price, (0, 0, 0, 0))

    def test_codex_still_requires_api_key(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(SystemExit, 'LLM_API_KEY'):
                load(require_token=False)

    def test_rejects_unknown_backend(self):
        with patch.dict(os.environ, {'CGM_BACKEND': 'unknown'}, clear=True):
            with self.assertRaisesRegex(SystemExit, 'CGM_BACKEND'):
                load(require_token=False)

    def test_separate_image_api_can_still_be_configured(self):
        with patch.dict(os.environ, {'CGM_BACKEND': 'antigravity', 'LLM_API_KEY': 'test'},
                        clear=True):
            self.assertEqual(load(require_token=False).image_model, 'gpt-image-2')


class AntigravityEventsTests(unittest.TestCase):
    def test_uses_final_totals_without_double_counting_step_usage(self):
        step = {'event': 'step_update', 'step_update': {
            'step_index': 1, 'state': 'DONE', 'step_type': 'agent_response',
            'usage': {'input_tokens': 100}}}
        res = parse_antigravity_events(events(
            {'event': 'step_update', 'step_update': {
                'step_index': 0, 'state': 'DONE', 'step_type': 'user_input'}},
            step, step,
            {'event': 'result', 'result': {
                'status': 'SUCCESS', 'response': 'ok', 'usage': {
                    'input_tokens': 100, 'cache_read_tokens': 40,
                    'output_tokens': 20, 'thinking_tokens': 10}}}))
        self.assertEqual(res, {'completed': True, 'error': None, 'last': 'ok',
                              'steps': 1, 'usage': {'input': 60, 'cache_read': 40,
                                                   'cache_write': 0, 'output': 20}})

    def test_failure_and_truncated_stream(self):
        res = parse_antigravity_events(events({'event': 'result', 'result': {
            'status': 'ERROR', 'error': 'authentication required'}}))
        self.assertFalse(res['completed'])
        self.assertEqual(res['error'], 'authentication required')
        self.assertFalse(parse_antigravity_events(b'noise\n{"event":')['completed'])


class AntigravityRunnerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        (self.root / 'AGENTS.md').write_text('Read images with view_image.')
        with patch.dict(os.environ, {'CGM_BACKEND': 'antigravity'}, clear=True):
            cfg = replace(load(require_token=False), work=self.root, db=self.root/'db',
                          mcp_python=sys.executable, run_timeout=0.01)
        self.store = Store(cfg.db)
        self.addCleanup(self.store.db.close)
        self.api = ToolAPI(None, self.store, 0)
        self.runner = Runner(cfg, self.store, None, self.api, AsyncMock())

    def test_run_configs_keep_tokens_and_rules_separate(self):
        dirs = [self.runner._new_run_dir(cid) for cid in (1, 2)]
        contexts = [RunCtx(rid, cid, path, 5) for cid, (rid, path) in enumerate(dirs, 1)]
        for ctx in contexts:
            cmd = self.runner._command(ctx.run_dir, ctx)
            self.assertEqual(cmd[0], 'agy')
            self.assertIn('--disable-slash-commands', cmd)
            self.assertNotIn('--model', cmd)
            data = json.loads((ctx.run_dir/'.agents/mcp_config.json').read_text())
            self.assertEqual(data['mcpServers']['telegram']['env']['CGM_RUN_TOKEN'], ctx.token)
            self.assertIn('view_file', (ctx.run_dir/'.agents/AGENTS.md').read_text())
        self.assertNotEqual(contexts[0].token, contexts[1].token)
        self.runner.cfg = replace(self.runner.cfg, model='test-model')
        self.assertEqual(self.runner._command(contexts[0].run_dir)[-2:],
                         ['--model', 'test-model'])

    async def test_timeout_and_cancellation_release_tokens_and_kill_process(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                entered, released = asyncio.Event(), asyncio.Event()

                async def communicate(data=None):
                    entered.set()
                    await released.wait()
                    return b'', b''

                proc = AsyncMock()
                proc.returncode = None
                proc.communicate.side_effect = communicate

                async def kill(p):
                    p.returncode = -9
                    released.set()

                self.runner._spawn = AsyncMock(return_value=proc)
                self.runner._kill = AsyncMock(side_effect=kill)
                rid, path = self.runner._new_run_dir(1)
                task = asyncio.create_task(self.runner.execute(1, rid, path, 'test'))
                await entered.wait()
                if cancel:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    await task
                    self.assertEqual(self.store.last_runs(1)[0]['error'], 'timeout')
                self.runner._kill.assert_awaited_once_with(proc)
                self.assertEqual(self.api.runs, {})
                self.assertEqual(self.runner.running, {})

    async def test_successful_exit_without_result_is_recorded_as_error(self):
        proc = AsyncMock()
        proc.returncode = 0
        proc.communicate.return_value = (b'{"event":"init"}\n', b'')
        self.runner._spawn = AsyncMock(return_value=proc)
        rid, path = self.runner._new_run_dir(1)
        res = await self.runner.execute(1, rid, path, 'test')
        self.assertIn('完成事件', res['error'])
        self.assertEqual(self.api.runs, {})

    async def test_command_setup_failure_releases_token(self):
        (self.root / 'AGENTS.md').unlink()
        rid, path = self.runner._new_run_dir(1)
        with self.assertRaises(FileNotFoundError):
            await self.runner.execute(1, rid, path, 'test')
        self.assertEqual(self.api.runs, {})
        self.assertEqual(self.runner.running, {})

    async def test_cli_environment_excludes_service_credentials(self):
        env = {'PATH': '/bin', 'HOME': str(self.root), 'LLM_API_KEY': 'image-key',
               'TELEGRAM_BOT_TOKEN': 'bot-secret', 'CGM_PASSPHRASE': 'session-secret',
               'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/tmp/test-bus'}
        with patch.dict(os.environ, env, clear=True), patch(
                'cgm.agent.asyncio.create_subprocess_exec', new_callable=AsyncMock) as spawn:
            await self.runner._spawn(['agy'], self.root)
        passed = spawn.await_args.kwargs['env']
        self.assertEqual(passed['HOME'], str(self.root))
        self.assertIn('DBUS_SESSION_BUS_ADDRESS', passed)
        for name in ('LLM_API_KEY', 'TELEGRAM_BOT_TOKEN', 'CGM_PASSPHRASE', 'CODEX_HOME'):
            self.assertNotIn(name, passed)


if __name__ == '__main__':
    unittest.main()
