import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from workbot.agents.codeagent import AgentResult
from workbot.agents.manager import AgentManager, _PLAN_RE, _json_object_from_text
from workbot.conversation.manager import ConversationManager
from workbot.storage.sqlite import Store


class WorkflowPlanOutputTests(unittest.IsolatedAsyncioTestCase):
    def test_extracts_tagged_json_with_leading_prose(self):
        text = 'planner note\n<WORKBOT_PLAN>{"summary":"ok","steps":[]}</WORKBOT_PLAN>\n'
        obj = _json_object_from_text(text, tag_re=_PLAN_RE)
        self.assertEqual(obj["summary"], "ok")

    def test_extracts_json_fence_embedded_in_prose(self):
        text = 'Here is the plan:\n```json\n{"summary":"ok","steps":[]}\n```\nDone.'
        obj = _json_object_from_text(text, tag_re=_PLAN_RE)
        self.assertEqual(obj["summary"], "ok")

    def test_extracts_bare_json_embedded_after_prefix(self):
        text = 'RESULT: {"summary":"ok","steps":[]}'
        obj = _json_object_from_text(text, tag_re=_PLAN_RE)
        self.assertEqual(obj["summary"], "ok")

    def test_empty_output_has_useful_error(self):
        with self.assertRaisesRegex(ValueError, "empty output"):
            _json_object_from_text("", tag_re=_PLAN_RE)

    async def test_plan_workflow_retries_once_after_invalid_output(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'AGENTS.md').write_text('test workspace', encoding='utf-8')
            db = Store(root / 'state.db')
            conversations = ConversationManager(db)
            manager = AgentManager({'command':'codeagent'}, root, conversations)
            valid = '<WORKBOT_PLAN>' + json.dumps({
                'summary': 'read both files',
                'steps': [
                    {'id':'local','executor':'windows','node':None,'instruction':'read local','depends_on':[], 'notify_on_complete':False,'milestone':''},
                    {'id':'remote','executor':'remote','node':'linux-server1','instruction':'read remote','depends_on':[], 'notify_on_complete':False,'milestone':''},
                ]
            }) + '</WORKBOT_PLAN>'
            manager.backend.run = mock.AsyncMock(side_effect=[
                AgentResult(text='I cannot provide JSON right now'),
                AgentResult(text=valid),
            ])
            plan = await manager.plan_workflow('c1', 'read two files', {'linux-server1': {'ssh_alias':'linux-server1'}})
            self.assertEqual(plan.summary, 'read both files')
            self.assertEqual(len(plan.steps), 2)
            self.assertEqual(manager.backend.run.await_count, 2)

    async def test_plan_workflow_reports_both_invalid_outputs(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = Store(root / 'state.db')
            conversations = ConversationManager(db)
            manager = AgentManager({'command':'codeagent'}, root, conversations)
            manager.backend.run = mock.AsyncMock(side_effect=[
                AgentResult(text='not json first'),
                AgentResult(text='not json second'),
            ])
            with self.assertRaisesRegex(ValueError, '两次都未返回可解析'):
                await manager.plan_workflow('c1', 'read two files', {'linux-server1': {'ssh_alias':'linux-server1'}})
            self.assertEqual(manager.backend.run.await_count, 2)

    async def test_plan_prompt_puts_current_request_near_top(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / 'AGENTS.md').write_text('workspace rules', encoding='utf-8')
            db = Store(root / 'state.db')
            conversations = ConversationManager(db)
            manager = AgentManager({'command':'codeagent'}, root, conversations)
            valid = '<WORKBOT_PLAN>' + json.dumps({
                'summary': 'ok',
                'steps': [
                    {'id':'s1','executor':'windows','node':None,'instruction':'read local','depends_on':[], 'notify_on_complete':False,'milestone':''},
                ]
            }) + '</WORKBOT_PLAN>'
            manager.backend.run = mock.AsyncMock(return_value=AgentResult(text=valid))
            request = r'读取Windows本地 C:\\example\\workbot\\state\\workflow-input.txt'
            await manager.plan_workflow('c1', request, {'linux-server1': {'ssh_alias':'linux-server1'}})
            sent_prompt = manager.backend.run.await_args.args[0]
            self.assertIn(request, sent_prompt)
            self.assertLess(sent_prompt.find(request), sent_prompt.find('--- Workspace instructions ---'))
            self.assertIn('CURRENT USER REQUEST (authoritative)', sent_prompt)

    async def test_repair_prompt_repeats_original_request(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db = Store(root / 'state.db')
            conversations = ConversationManager(db)
            manager = AgentManager({'command':'codeagent'}, root, conversations)
            valid = '<WORKBOT_PLAN>' + json.dumps({
                'summary': 'ok',
                'steps': [
                    {'id':'s1','executor':'windows','node':None,'instruction':'read local','depends_on':[], 'notify_on_complete':False,'milestone':''},
                ]
            }) + '</WORKBOT_PLAN>'
            manager.backend.run = mock.AsyncMock(side_effect=[AgentResult(text='bad'), AgentResult(text=valid)])
            request = '读取Windows文件并让linux-server1读取Linux文件'
            await manager.plan_workflow('c1', request, {'linux-server1': {'ssh_alias':'linux-server1'}})
            repair_prompt = manager.backend.run.await_args_list[1].args[0]
            self.assertIn(request, repair_prompt)
            self.assertIn('Do NOT ask what the task is', repair_prompt)


if __name__ == '__main__':
    unittest.main()
