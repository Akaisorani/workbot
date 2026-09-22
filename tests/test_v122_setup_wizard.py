import json
from pathlib import Path

from workbot.setup.configurator import backup_config, load_config, merge_config, save_config_atomic
from workbot.setup.doctor import run_doctor


def test_atomic_save_and_load(tmp_path: Path):
    p=tmp_path/'config'/'local.json'; save_config_atomic(p,{'a':1})
    assert load_config(p)=={'a':1}; assert not p.with_name('local.json.tmp').exists()


def test_backup_keeps_bounded_count(tmp_path: Path):
    p=tmp_path/'local.json'; p.write_text('{}',encoding='utf-8')
    for i in range(4):
        b=backup_config(p,keep=2); b.touch()
    assert len(list(tmp_path.glob('local.json.bak-*'))) <= 2


def test_recursive_merge_preserves_existing_nested_values():
    assert merge_config({'a':{'x':1,'y':2}},{'a':{'y':3}})=={'a':{'x':1,'y':3}}


def test_doctor_json_shape_and_disabled_optional_features(tmp_path: Path, monkeypatch):
    (tmp_path/'config').mkdir(); (tmp_path/'AGENTS.example.md').write_text('x',encoding='utf-8')
    cfg={'database':'state/workbot.db','im':{'cli':'missing-welink','self_accounts':['u'],'intent':{'bot_aliases':['bot']}},'agent':{'command':'missing-codeagent'},'nodes':{},'rag':{'enabled':False}}
    p=tmp_path/'config'/'local.json'; p.write_text(json.dumps(cfg),encoding='utf-8')
    result=run_doctor(p,workspace=tmp_path)
    assert isinstance(result['checks'],list) and result['exit_code'] in {0,1,2}
    rag=next(x for x in result['checks'] if x['name']=='rag'); assert rag['skipped'] is True
