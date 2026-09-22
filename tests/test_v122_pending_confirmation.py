import json
from pathlib import Path
from unittest import mock

import pytest

from workbot.conversation.models import IncomingMessage, PendingAction
from workbot.main import WorkBot


class FakeIM:
    def __init__(self): self.sent=[]
    async def send_text(self, conversation_id, text): self.sent.append((conversation_id,text))


def make_bot(tmp_path: Path):
    (tmp_path/'config').mkdir()
    cfg={
        'database':'state/workbot.db',
        'im':{'cli':'welink-cli','groups':[], 'intent':{'require_alias':True,'bot_aliases':['bot']}},
        'agent':{'command':'codeagent'},
        'execution_policy':{'default':'deny','allow_senders':['owner','other']},
        'reply_policy':{'default':'deny','allow_senders':['owner','other']},
        'nodes':{'linux-server1':{'ssh_alias':'linux-server1','enabled':True}},
        'default_node':'linux-server1',
    }
    p=tmp_path/'config'/'local.json'; p.write_text(json.dumps(cfg),encoding='utf-8')
    bot=WorkBot(p); bot.im=FakeIM(); return bot


def msg(mid, text, sender='owner'):
    return IncomingMessage(platform='welink',conversation_id='welink:group:g',conversation_kind='group',external_conversation_id='g',external_message_id=str(mid),sender_id=sender,content=text,sent_at_ms=mid)


@pytest.mark.asyncio
@pytest.mark.parametrize('word',['创建','确认','好的','yes','y','ok！'])
async def test_pending_natural_confirm_bypasses_alias(tmp_path, word):
    bot=make_bot(tmp_path)
    bot.conversations.ensure(msg(0,'init'))
    pending=PendingAction('task.steer', {'task_id':'task-abcdef','instruction':'x','authorized_by':'owner','authorization_message_id':'0'}, '是否执行？')
    bot.conversations.set_pending_action(msg(0,'').conversation_id,pending)
    with mock.patch.object(bot,'_execute_pending',new=mock.AsyncMock()) as run:
        await bot.handle_im_message(msg(1,word))
    run.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize('word',['取消','不创建','no','n。'])
async def test_pending_natural_cancel_bypasses_alias(tmp_path, word):
    bot=make_bot(tmp_path); bot.conversations.ensure(msg(0,'init'))
    bot.conversations.set_pending_action(msg(0,'').conversation_id, PendingAction('task.create',{'authorized_by':'owner'},'是否创建？'))
    await bot.handle_im_message(msg(1,word))
    assert bot.conversations.get_pending_action(msg(0,'').conversation_id) is None
    assert '已取消待执行操作' in bot.im.sent[-1][1]


@pytest.mark.asyncio
async def test_no_pending_bare_create_remains_store_only(tmp_path):
    bot=make_bot(tmp_path)
    with mock.patch.object(bot.agents,'answer',new=mock.AsyncMock()) as answer:
        await bot.handle_im_message(msg(1,'创建'))
    answer.assert_not_awaited(); assert bot.im.sent == []


@pytest.mark.asyncio
async def test_other_sender_cannot_take_pending(tmp_path):
    bot=make_bot(tmp_path); bot.conversations.ensure(msg(0,'init'))
    p=PendingAction('task.create',{'authorized_by':'owner','authorization_message_id':'0'},'是否创建？')
    bot.conversations.set_pending_action(msg(0,'').conversation_id,p)
    await bot.handle_im_message(msg(1,'创建',sender='other'))
    assert bot.conversations.get_pending_action(msg(0,'').conversation_id) is not None
    assert bot.im.sent == []  # common group chatter must not reveal somebody else's pending action
