import os
import sys
from pathlib import Path

import pytest

from workbot.agents.codeagent import CodeAgentBackend


@pytest.mark.asyncio
async def test_streaming_run_tracks_output_and_completion(tmp_path: Path):
    code="import sys,time; print('line1',flush=True); time.sleep(.05); print('line2',flush=True)"
    b=CodeAgentBackend({'command':sys.executable,'new_args':['-u','-c',code],'prompt_args':[],'timeout_seconds':3,'observability':{'recent_output_lines':10}},tmp_path)
    r=await b.run('ignored',conversation_id='c1')
    st=b.latest_run('c1')
    assert r.returncode==0 and st is not None and st.status=='completed' and st.pid
    assert st.last_output_at is not None and any('line2' in x for x in st.recent_output)


@pytest.mark.asyncio
async def test_ring_buffer_is_bounded(tmp_path: Path):
    code="[print('x'*20+str(i),flush=True) for i in range(20)]"
    b=CodeAgentBackend({'command':sys.executable,'new_args':['-u','-c',code],'prompt_args':[],'timeout_seconds':3,'observability':{'recent_output_lines':3,'recent_output_max_chars':200}},tmp_path)
    await b.run('x',conversation_id='c1'); st=b.latest_run('c1')
    assert len(st.recent_output)<=3 and any('19' in x for x in st.recent_output)


@pytest.mark.asyncio
async def test_timeout_retains_tail_and_marks_timed_out(tmp_path: Path):
    if os.name == "nt":
        code="import time; print('before-hang',flush=True); time.sleep(5)"
        command, args = sys.executable, ['-u','-c',code]
    else:
        # Some hermetic Python 3.13 test runners do not expose incremental
        # stdout from a nested copy of the same interpreter through asyncio
        # pipes. A shell child still exercises the WorkBot streaming/timeout
        # path itself without depending on that interpreter quirk.
        command, args = '/bin/sh', ['-c', 'echo before-hang; sleep 5']
    b=CodeAgentBackend({'command':command,'new_args':args,'prompt_args':[],'timeout_seconds':1},tmp_path)
    with pytest.raises(RuntimeError) as exc:
        await b.run('x',conversation_id='c1')
    st=b.latest_run('c1')
    assert st.status=='timed_out' and 'before-hang' in '\n'.join(st.recent_output)
    assert 'run_id=arun-' in str(exc.value)


@pytest.mark.asyncio
async def test_stdout_task_completed_is_only_observation(tmp_path: Path):
    code="print('Task completed',flush=True)"
    b=CodeAgentBackend({'command':sys.executable,'new_args':['-u','-c',code],'prompt_args':[]},tmp_path)
    await b.run('x',conversation_id='c1')
    st=b.latest_run('c1'); assert st.status=='completed' and 'Task completed' in '\n'.join(st.recent_output)
