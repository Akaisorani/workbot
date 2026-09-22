from pathlib import Path

from workbot.setup.agents_generator import build_agents_context, generate_agents_file, needs_regeneration


def cfg():
    return {
        'default_node':'linux-server1',
        'nodes':{'linux-server1':{'ssh_alias':'ls1','enabled':True,'workspace':'/home/workbot/workspace'}},
        'im':{'self_accounts':['example-user-b'],'intent':{'bot_aliases':['bot']},'rich_message':{'image':{'enabled':True}}},
        'agent':{'gaussdb_context':{'source_root':'C:\\example\\source','manual_root':'C:\\example\\manuals','wiki_entry_url':'https://wiki.example.com/x'},'token':'excluded-example-value-a'},
        'rag':{'enabled':True},
        'api_secret':'excluded-example-value-b',
    }


def test_allowlisted_context_substitution_and_no_secret(tmp_path: Path):
    t=tmp_path/'AGENTS.example.md'; o=tmp_path/'AGENTS.md'
    t.write_text('{{DEFAULT_NODE}}\n{{NODE_LIST}}\n{{SOURCE_ROOTS}}\n{{WIKI_URL}}\n{{RAG_ENABLED}}',encoding='utf-8')
    generate_agents_file(t,cfg(),o)
    text=o.read_text(encoding='utf-8')
    assert 'linux-server1' in text and 'C:\\example\\source' in text and 'wiki.example.com' in text
    assert 'excluded-example-value-a' not in text and 'excluded-example-value-b' not in text
    assert 'template_sha256:' in text and 'config_sha256:' in text


def test_missing_optional_fields_render_not_configured(tmp_path: Path):
    t=tmp_path/'AGENTS.example.md'; o=tmp_path/'AGENTS.md'; t.write_text('{{WIKI_URL}}|{{MANUAL_PATHS}}',encoding='utf-8')
    generate_agents_file(t,{},o)
    assert 'Not configured' in o.read_text(encoding='utf-8')


def test_template_or_relevant_config_change_requires_regeneration(tmp_path: Path):
    t=tmp_path/'AGENTS.example.md'; o=tmp_path/'AGENTS.md'; t.write_text('{{DEFAULT_NODE}}',encoding='utf-8')
    c=cfg(); generate_agents_file(t,c,o); assert not needs_regeneration(t,c,o)
    c['default_node']='linux-server2'; assert needs_regeneration(t,c,o)
    generate_agents_file(t,c,o); t.write_text('{{DEFAULT_NODE}} changed',encoding='utf-8'); assert needs_regeneration(t,c,o)


def test_manual_agents_is_backed_up(tmp_path: Path):
    t=tmp_path/'AGENTS.example.md'; o=tmp_path/'AGENTS.md'; t.write_text('{{BOT_ALIASES}}',encoding='utf-8'); o.write_text('manual',encoding='utf-8')
    generate_agents_file(t,cfg(),o)
    assert (tmp_path/'AGENTS.md.bak-v1122').read_text(encoding='utf-8') == 'manual'
