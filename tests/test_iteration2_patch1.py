"""Offline regression tests. No Telegram token, database or external network needed."""
import ast
import logging
import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from iteration2_ux import (valid_full_name, latest_suggestion_due, suggestion_batches,
                           telegram_units, SUGGESTION_MAX_LENGTH)

TREE = ast.parse((ROOT / 'main.py').read_text(encoding='utf-8'))
TZ = ZoneInfo('Europe/Istanbul')


def load(*names, **env):
    """Run exact function bodies from the patched main without importing its live bot."""
    nodes = []
    for node in TREE.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names:
            import copy
            node = copy.deepcopy(node)
            node.decorator_list = []
            nodes.append(node)
    assert len(nodes) == len(names), names
    module = ast.Module(body=[ast.ImportFrom(module='__future__', names=[ast.alias(name='annotations')], level=0), *nodes], type_ignores=[])
    ns = dict(valid_full_name=valid_full_name, latest_suggestion_due=latest_suggestion_due,
              suggestion_batches=suggestion_batches, TZ=TZ, **env)
    exec(compile(ast.fix_missing_locations(module), 'main-functions', 'exec'), ns)
    return ns


class FakePool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class FakeConnection:
    def __init__(self, *, rows=None, row_results=None):
        self.fetch = AsyncMock(return_value=rows or [])
        self.fetchrow = AsyncMock(side_effect=row_results or [])
        self.execute = AsyncMock(return_value='UPDATE 1')

    @asynccontextmanager
    async def transaction(self, **kwargs):
        yield


def profile(**kwargs):
    return dict(status='active', deleted_at=None, removed_by_admin=0,
                display_name='Анна Иванова', onboarding_step='completed',
                profile_migration_required=0, **kwargs)


class PureTests(unittest.TestCase):
    def test_name_accepts_multilingual_and_compound_names(self):
        for name in ['Анна Иванова', 'Мария-Анна Иванова', 'Jean de la Cruz', 'Олег   Петров']:
            self.assertTrue(valid_full_name(name), name)

    def test_name_rejects_incomplete_or_excessive(self):
        for name in [None, '', 'Рома', 'Анна\nИванова', 'Анна\rИванова', 'A ' + 'B'*79]:
            self.assertFalse(valid_full_name(name), name)

    def test_name_is_completeness_only_not_identity_validation(self):
        self.assertTrue(valid_full_name('Анна 123'))  # identity still belongs to moderation

    def test_due_dates_and_month_boundary(self):
        def d(y,m,day,h): return datetime(y,m,day,h,tzinfo=TZ)
        for now, expected in [(d(2026,12,1,9), d(2026,11,15,10)),
                              (d(2026,12,1,10), d(2026,12,1,10)),
                              (d(2026,12,14,23), d(2026,12,1,10)),
                              (d(2026,12,15,9), d(2026,12,1,10)),
                              (d(2026,12,15,10), d(2026,12,15,10)),
                              (d(2027,1,1,0), d(2026,12,15,10))]:
            self.assertEqual(latest_suggestion_due(now), expected)

    def test_batch_does_not_split_or_lose_entries(self):
        entries = [(i, 'А'*1500) for i in range(5)] + [(7, '😀'*1500)]
        batches = suggestion_batches(entries)
        self.assertEqual([x for b in batches for x in b], entries)
        for b in batches:
            self.assertLessEqual(telegram_units('\n\n'.join(x[1] for x in b)), 3800)
        self.assertEqual(suggestion_batches([]), [])

    def test_help_legacy_profile_not_restricted(self):
        ns = load('user_help_text', 'access_denied_message')
        u = profile(); u['display_name'] = 'Рома'
        t = ns['user_help_text'](u)
        self.assertIn('/suggest', t)
        self.assertIn('добавь фамилию', t)
        self.assertIn('/thanks', t)
        u['status'] = 'access_denied'
        self.assertNotIn('/suggest', ns['user_help_text'](u))
        self.assertIn('команды бренд-коммуникаций', ns['user_help_text'](u))

    def test_match_card_with_optional_team(self):
        ns = load('match_profile_block', 'clean_name', team_label=lambda k: 'Коммерция' if k=='commerce' else '')
        u = dict(display_name='Анна Иванова', team='commerce', org_team_name='Продажи', responsibility_text='Работа с клиентами', username='anna')
        t = ns['match_profile_block'](u)
        self.assertIn('Департамент: Коммерция', t)
        self.assertIn('Команда: Продажи', t)
        self.assertIn('@anna', t)
        u['org_team_name'] = None
        self.assertNotIn('Команда:', ns['match_profile_block'](u))

    def test_feedback_permissions(self):
        ns = load('suggestion_allowed')
        for status in ['active','paused','pending_activation','access_denied','self_deleted']:
            u=profile(); u['status']=status
            self.assertEqual(ns['suggestion_allowed'](u), status in ['active','paused'])
        u=profile(); u['removed_by_admin']=1
        self.assertFalse(ns['suggestion_allowed'](u))
        self.assertFalse(ns['suggestion_allowed'](None))


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_activation_blocks_incomplete_and_allows_complete(self):
        for name, confirmed, expected in [('Рома',1,False),('Рома Иванов',0,False),('Рома Иванов',1,True)]:
            conn=FakeConnection(row_results=[dict(display_name=name,name_confirmed=confirmed)])
            ns=load('activate_pending_user', pool=FakePool(conn))
            self.assertEqual(await ns['activate_pending_user'](1), expected)
            self.assertEqual(conn.execute.await_count, int(expected))

    async def test_personal_menu_failure_does_not_abort_next_admin(self):
        bot=SimpleNamespace(set_my_commands=AsyncMock(side_effect=[None, RuntimeError('chat not found'), None]))
        ns=load('configure_bot_commands', bot=bot, USER_BOT_COMMANDS=[], ADMIN_USER_IDS=[1,2],
                BotCommand=lambda **kw: kw, BotCommandScopeAllPrivateChats=lambda: 'all',
                BotCommandScopeChat=lambda **kw: kw, logging=Mock())
        await ns['configure_bot_commands']()
        self.assertEqual(bot.set_my_commands.await_count, 3)

    async def test_global_menu_failure_also_does_not_abort(self):
        bot=SimpleNamespace(set_my_commands=AsyncMock(side_effect=[RuntimeError('network'), None]))
        ns=load('configure_bot_commands', bot=bot, USER_BOT_COMMANDS=[], ADMIN_USER_IDS=[1],
                BotCommand=lambda **kw: kw, BotCommandScopeAllPrivateChats=lambda: 'all',
                BotCommandScopeChat=lambda **kw: kw, logging=Mock())
        await ns['configure_bot_commands']()
        self.assertEqual(bot.set_my_commands.await_count, 2)

    async def test_save_feedback_and_clear_dialog(self):
        conn=FakeConnection(row_results=[dict(flow_type='improvement_suggestion',step='waiting_text'),profile()])
        ns=load('save_suggestion','suggestion_allowed',pool=FakePool(conn))
        self.assertTrue(await ns['save_suggestion'](1,'Идея'))
        self.assertEqual(conn.execute.await_count,2)
        self.assertIn('INSERT INTO improvement_suggestions',conn.execute.await_args_list[0].args[0])
        self.assertIn('DELETE FROM user_flows',conn.execute.await_args_list[1].args[0])

    async def test_no_feedback_insert_from_stale_or_denied_flow(self):
        conn=FakeConnection(row_results=[None]); ns=load('save_suggestion','suggestion_allowed',pool=FakePool(conn))
        self.assertFalse(await ns['save_suggestion'](1,'Идея'))
        conn.execute.assert_not_awaited()
        u=profile();u['status']='access_denied'
        conn=FakeConnection(row_results=[dict(flow_type='improvement_suggestion',step='waiting_text'),u])
        ns=load('save_suggestion','suggestion_allowed',pool=FakePool(conn))
        self.assertFalse(await ns['save_suggestion'](1,'Идея'))
        self.assertEqual(conn.execute.await_count,1)
        self.assertIn('DELETE FROM user_flows',conn.execute.await_args.args[0])

    async def test_archive_rejects_non_owner(self):
        cb=SimpleNamespace(from_user=SimpleNamespace(id=2),answer=AsyncMock(),data='admin_suggestions:0')
        send=AsyncMock()
        ns=load('admin_suggestions_callback',is_owner=lambda _: False,send_suggestion_archive=send)
        await ns['admin_suggestions_callback'](cb)
        send.assert_not_awaited()
        cb.answer.assert_awaited_once_with('Нет доступа.',show_alert=True)

    async def test_full_archive_rejects_non_owner(self):
        cb=SimpleNamespace(from_user=SimpleNamespace(id=2),answer=AsyncMock())
        ns=load('admin_suggestions_all_callback',is_owner=lambda _: False,pool=None)
        await ns['admin_suggestions_all_callback'](cb)
        cb.answer.assert_awaited_once_with('Нет доступа.',show_alert=True)

    async def test_manual_archive_never_marks_reported(self):
        cb=SimpleNamespace(from_user=SimpleNamespace(id=1),answer=AsyncMock())
        conn=FakeConnection(rows=[dict(id=1,body='Идея')]);send=AsyncMock()
        ns=load('admin_suggestions_all_callback',is_owner=lambda _: True,pool=FakePool(conn),
                SUGGESTION_SELECT='SELECT',suggestion_entry=lambda row:row['body'],
                safe_send_message=send,ADMIN_ID=1)
        await ns['admin_suggestions_all_callback'](cb)
        self.assertIn('Идея',send.await_args.args[1])
        conn.execute.assert_not_awaited()

    async def test_summary_mark_only_delivered_batches(self):
        rows=[dict(id=i,body='А'*1500) for i in range(1,4)]
        conn=FakeConnection(rows=rows)
        send=AsyncMock(side_effect=[None,RuntimeError('network')])
        ns=load('send_suggestion_summary', pool=FakePool(conn),SUGGESTION_SELECT='SELECT',
                suggestion_entry=lambda r:r['body'],safe_send_message=send,ADMIN_ID=1)
        with self.assertRaises(RuntimeError):
            await ns['send_suggestion_summary'](datetime(2026,12,1,10,tzinfo=TZ))
        self.assertEqual(conn.execute.await_count,1)
        self.assertEqual(conn.execute.await_args.args[1],[1])

    async def test_empty_summary_has_no_telegram_message(self):
        conn=FakeConnection(); send=AsyncMock()
        ns=load('send_suggestion_summary',pool=FakePool(conn),SUGGESTION_SELECT='SELECT',
                suggestion_entry=lambda r:r['body'],safe_send_message=send,ADMIN_ID=1)
        await ns['send_suggestion_summary'](datetime(2026,12,1,10,tzinfo=TZ))
        send.assert_not_awaited()

    async def test_summary_scheduler_deduplicates_completed_period(self):
        status=AsyncMock(return_value='completed'); run=AsyncMock()
        ns=load('run_due_suggestion_summary',get_job_status=status,run_job_once=run,send_suggestion_summary=AsyncMock())
        await ns['run_due_suggestion_summary'](datetime(2026,12,15,11,tzinfo=TZ))
        status.assert_awaited_once_with('improvement_suggestions_summary','2026-12-15')
        run.assert_not_awaited()
        status.return_value=None
        await ns['run_due_suggestion_summary'](datetime(2026,12,15,11,tzinfo=TZ))
        self.assertEqual(run.await_args.args[:2],('improvement_suggestions_summary','2026-12-15'))


if __name__ == '__main__':
    unittest.main(verbosity=2)
