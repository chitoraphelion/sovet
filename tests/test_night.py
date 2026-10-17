import json
from unittest.mock import patch

import pytest

from sovet.core import Project
from sovet.night import NightRun
from sovet.runtime import worker_lock


@pytest.fixture
def project(tmp_path):
    p = Project.init(tmp_path)
    (tmp_path / 'RESEARCH_BRIEF.md').write_text('Research task with precise assumptions')
    return p


def complete_turn(p, role, output, controller):
    job = p.claim(role)
    if role == 'claude':
        (p.root / 'NIGHT_REPORT.md').write_text('Checkpoint with test evidence')
    p.finish(job, f'{role} finished')
    return {'id': job['id'], 'ok': True}


def test_bounded_writer_reviewer_and_receipt(project):
    roles = []
    def execute(p, role, **kwargs):
        roles.append(role)
        return complete_turn(p, role, **kwargs)
    with patch('sovet.night.execute_next', side_effect=execute), patch('sovet.night.sync_comments'):
        report = NightRun(project, 'RESEARCH_BRIEF.md', max_turns=4, output=lambda _: None).run()
    assert roles == ['claude', 'codex', 'claude', 'codex']
    assert report['turns'] == 4 and report['reason'] == 'max_turns'
    assert NightRun(project, 'RESEARCH_BRIEF.md').pending()['origin'] == 'peer-result'
    assert json.loads((project.team / 'night/latest.json').read_text()) == report
    assert not any(j['status'] == 'running' for j in project.jobs())


def test_does_not_duplicate_peer_review(project):
    def execute(p, role, **kwargs):
        job = p.claim(role)
        p.enqueue('codex', 'writer requested review', origin='claude', read_only=True, reply_to='claude')
        p.finish(job, 'ready')
        return {'id': job['id'], 'ok': True}
    with patch('sovet.night.execute_next', side_effect=execute), patch('sovet.night.sync_comments'):
        NightRun(project, 'RESEARCH_BRIEF.md', max_turns=1, output=lambda _: None).run()
    assert len([j for j in project.jobs() if j['role'] == 'codex']) == 1


def test_role_lease_blocks_night_without_model_calls(project):
    with worker_lock(project, 'codex'), patch('sovet.night.execute_next') as execute:
        with pytest.raises(ValueError, match='Роль занята'):
            NightRun(project, 'RESEARCH_BRIEF.md').run()
        execute.assert_not_called()
    assert project.jobs() == []


def test_failure_stops_and_remains_visible(project):
    def fail(p, role, **kwargs):
        job = p.claim(role)
        p.finish(job, error='usage_limit_reached')
        return {'id': job['id'], 'ok': False, 'error': 'usage_limit_reached'}
    with patch('sovet.night.execute_next', side_effect=fail) as execute, patch('sovet.night.sync_comments'):
        report = NightRun(project, 'RESEARCH_BRIEF.md', output=lambda _: None).run()
    assert execute.call_count == 1
    assert report['reason'] == 'failed' and report['error'] == 'usage_limit_reached'
    assert all(j['role'] == 'claude' for j in project.jobs())


def test_plan_is_readonly_and_validates_inputs(project):
    before = set(project.root.rglob('*'))
    assert NightRun(project, 'RESEARCH_BRIEF.md').plan()['writer'] == 'claude'
    assert set(project.root.rglob('*')) == before
    for hours in (0, -1, float('inf'), float('nan')):
        with pytest.raises(ValueError):
            NightRun(project, 'RESEARCH_BRIEF.md', hours=hours)
    with pytest.raises(ValueError):
        NightRun(project, '../outside.md')
    with pytest.raises(ValueError):
        NightRun(project, 'RESEARCH_BRIEF.md', max_turns=0)


def test_deadline_does_not_start_a_turn(project):
    # start + deadline, then loop clock already exceeds it
    with patch('sovet.night.time.monotonic', side_effect=[0, 4000]), patch('sovet.night.execute_next') as execute:
        report = NightRun(project, 'RESEARCH_BRIEF.md', hours=1, output=lambda _: None).run()
    assert report['reason'] == 'deadline' and report['turns'] == 0
    execute.assert_not_called()


def test_network_retry_preserves_review_routing_and_limits(project):
    project.enqueue('codex', 'review existing result', read_only=True, depth=1, reply_to='claude')
    attempts = []
    def execute(p, role, output, controller):
        job = p.claim(role)
        attempts.append(job)
        if len(attempts) == 1:
            p.finish(job, error='Connection reset by peer')
            return {'id': job['id'], 'ok': False, 'error': 'Connection reset by peer'}
        return_job = dict(job)
        p.finish(job, 'review finished')
        return {'id': return_job['id'], 'ok': True}
    runner = NightRun(project, 'RESEARCH_BRIEF.md', max_turns=2, output=lambda _: None)
    with patch('sovet.night.execute_next', side_effect=execute), patch.object(runner, 'wait_for_retry', side_effect=lambda *_:setattr(runner, 'reviewer_not_before', 0)) as wait:
        report = runner.run()
    assert [a['role'] for a in attempts] == ['codex', 'codex']
    assert attempts[1]['reply_to'] == 'claude' and attempts[1]['depth'] == 1 and attempts[1]['read_only']
    assert attempts[1]['origin'] == 'retry'
    assert wait.call_args.args[0] == pytest.approx(15, abs=1)
    assert report['reason'] == 'max_turns'
    assert runner.pending('claude')['origin'] == 'peer-result'


def test_network_retry_exhaustion_is_bounded(project):
    def fail(p, role, **kwargs):
        job = p.claim(role)
        p.finish(job, error='stream disconnected before completion')
        return {'id': job['id'], 'ok': False, 'error': 'stream disconnected before completion'}
    runner = NightRun(project, 'RESEARCH_BRIEF.md', max_turns=20, retries=2, output=lambda _: None)
    with patch('sovet.night.execute_next', side_effect=fail) as execute, patch.object(runner, 'wait_for_retry') as wait:
        report = runner.run()
    assert execute.call_count == 3 and wait.call_count == 2
    assert [call.args[0] for call in wait.call_args_list] == [15, 30]
    assert report['reason'] == 'failed'
    assert len(project.jobs()) == 3
    assert 'задачу #1' in project.jobs()[0]['prompt']
    assert project.jobs()[0]['prompt'].count('после временного отказа') == 1


def test_known_quota_reset_waits_without_treating_warning_as_rejection(project):
    runner = NightRun(project, 'RESEARCH_BRIEF.md')
    log = project.team / 'logs/claude-1.jsonl'
    log.parent.mkdir()
    job = {'id': 1, 'role': 'claude'}
    def event(status, reset):
        return json.dumps({'type': 'rate_limit_event', 'rate_limit_info': {'status': status, 'resetsAt': reset}})
    log.write_text(event('allowed_warning', 500))
    assert runner.retry_delay(job, 'usage_limit_reached') is None
    log.write_text(event('rejected', 500))
    with patch('sovet.night.time.time', return_value=100):
        assert runner.retry_delay(job, 'usage_limit_reached') == 460
    log.write_text(event('rejected', True))
    assert runner.retry_delay(job, 'usage_limit_reached') is None
    assert runner.retry_delay(job, 'permission denied') is None
    assert NightRun(project, 'RESEARCH_BRIEF.md', retries=0).retry_delay(job, 'connection reset') is None


def test_stop_while_waiting_preserves_queued_retry(project):
    def fail(p, role, **kwargs):
        job = p.claim(role)
        p.finish(job, error='Connection reset by peer')
        return {'id': job['id'], 'ok': False, 'error': 'Connection reset by peer'}
    runner = NightRun(project, 'RESEARCH_BRIEF.md', output=lambda _: None)
    with patch('sovet.night.execute_next', side_effect=fail) as execute, patch.object(runner, 'wait_for_retry', side_effect=lambda *_:runner.stop()):
        report = runner.run()
    assert report['reason'] == 'stopped' and report['error'] is None
    assert report['pending_job'] and execute.call_count == 1


def test_unavailable_reviewer_does_not_block_writer_and_is_not_duplicated(project):
    project.config['writer'] = 'codex'
    project.save_config()
    runner = NightRun(project, 'RESEARCH_BRIEF.md', max_turns=4,
                      reviewer_after='2099-01-01T20:31:00+03:00', output=lambda _:None)
    roles = []
    def execute(p, role, **kwargs):
        roles.append(role)
        if roles == ['codex', 'codex']:
            with p.db() as db:
                assert db.execute("SELECT count(*) FROM jobs WHERE role='claude' AND status='pending'").fetchone()[0] == 1
        if role == 'claude':
            assert runner.reviewer_not_before == 0
        return complete_turn(p, role, **kwargs)
    with patch('sovet.night.execute_next', side_effect=execute), patch.object(runner, 'wait_for_retry', side_effect=lambda *_:setattr(runner, 'reviewer_not_before', 0)) as wait:
        report = runner.run()
    assert roles == ['codex', 'codex', 'claude', 'codex']
    assert wait.call_count == 1 and report['error'] is None


def test_deferred_oldest_job_is_preserved_when_turn_budget_ends(project):
    project.config['writer'] = 'codex'
    project.save_config()
    review = project.enqueue('claude', 'independent review', read_only=True, reply_to='codex')
    runner = NightRun(project, 'RESEARCH_BRIEF.md', max_turns=1,
                      reviewer_after='2099-01-01T20:31:00+03:00', output=lambda _:None)
    roles=[]
    def execute(p, role, **kwargs):
        roles.append(role)
        return complete_turn(p, role, **kwargs)
    with patch('sovet.night.execute_next', side_effect=execute):
        report = runner.run()
    assert roles == ['codex']
    assert report['pending_job'] == review
    assert runner.pending('claude') is None
    assert runner.pending('claude', include_deferred=True)['id'] == review


def test_reviewer_known_quota_cooldown_keeps_writer_working(project):
    project.config['writer']='codex'
    project.save_config()
    roles=[]
    runner=NightRun(project, 'RESEARCH_BRIEF.md', max_turns=5, output=lambda _:None)
    def execute(p, role, **kwargs):
        roles.append(role)
        if role=='claude':
            job=p.claim(role)
            folder=p.team/'logs';folder.mkdir(exist_ok=True)
            (folder/f"claude-{job['id']}.jsonl").write_text(json.dumps({'type':'rate_limit_event','rate_limit_info':{'status':'rejected','resetsAt':4070908800}}))
            p.finish(job,error='usage_limit_reached')
            return {'id':job['id'],'ok':False,'error':'usage_limit_reached'}
        return complete_turn(p, role, **kwargs)
    with patch('sovet.night.execute_next',side_effect=execute), patch.object(runner,'wait_for_retry',side_effect=lambda *_:runner.stop()):
        report=runner.run()
    assert roles==['codex','claude','codex']
    assert report['reason']=='stopped' and report['error'] is None
    assert runner.pending('claude',include_deferred=True)


def test_reviewer_after_requires_explicit_timezone(project):
    for timestamp in ('tomorrow','2026-10-17T20:30:00'):
        with pytest.raises(ValueError,match='reviewer-after'):
            NightRun(project,'RESEARCH_BRIEF.md',reviewer_after=timestamp)


def test_rotation_changes_permissions_and_research_roles(project):
    from sovet.runtime import command, protocol
    from sovet.research import profile_context
    project.config.update(writer='codex',profile='math-research',review_instructions={'claude':'Short focused review only.'})
    with patch('sovet.runtime.shutil.which', side_effect=lambda name:'/tmp/'+name):
        coder, readonly=command(project,'codex',{'read_only':False},'saved-codex')
        reviewer, reviewonly=command(project,'claude',{'read_only':True},None)
    assert not readonly and coder[coder.index('-s')+1]=='workspace-write'
    assert reviewonly and 'Read,Grep,Glob,ToolSearch' in reviewer
    mcp=json.loads((project.team/'mcp-claude.json').read_text())
    assert '--read-only' in mcp['mcpServers']['sovet']['args']
    assert 'codex derivation/formalization' in profile_context(project)
    assert 'claude review' in profile_context(project)
    assert 'Short focused review only.' in protocol(project,'claude',True)
    assert 'Short focused review only.' not in protocol(project,'codex',False)


def test_turn_timeout_becomes_checkpoint_continuation_then_review(project):
    roles=[]
    prompts=[]
    def execute(p, role, **kwargs):
        roles.append(role)
        if len(roles)==1:
            job=p.claim(role)
            p.finish(job,error='Превышено время хода')
            return {'id':job['id'],'ok':False,'error':'Превышено время хода'}
        with p.db() as db:
            prompts.append(db.execute("SELECT prompt FROM jobs WHERE role=? AND status='pending' ORDER BY id LIMIT 1",(role,)).fetchone()[0])
        return complete_turn(p,role,**kwargs)
    runner=NightRun(project,'RESEARCH_BRIEF.md',max_turns=3,output=lambda _:None)
    with patch('sovet.night.execute_next',side_effect=execute):
        result=runner.run()
    assert roles==['claude','claude','codex']
    assert 'ТОЛЬКО восстановление checkpoint' in prompts[0]
    assert result['reason']=='max_turns' and result['error'] is None
    assert any(j['status']=='failed' for j in project.jobs())


def test_inflight_checkpoint_exposes_job_and_completed_turn_count(project):
    def execute(p,role,**kwargs):
        state=json.loads((p.team/'night/latest.json').read_text())
        assert state['reason']=='running' and state['active_role']==role
        assert state['active_job']==1 and state['turns']==0
        return complete_turn(p,role,**kwargs)
    with patch('sovet.night.execute_next',side_effect=execute):
        NightRun(project,'RESEARCH_BRIEF.md',max_turns=1,output=lambda _:None).run()


def test_reviewer_timeout_continuation_never_requests_file_writes(project):
    job_id=project.enqueue('codex','review lemma',read_only=True,reply_to='claude')
    job=project.claim('codex')
    project.finish(job,error='Превышено время хода')
    runner=NightRun(project,'RESEARCH_BRIEF.md')
    retry_id=runner.queue_retry(job,checkpoint_only=True)
    retry=runner.pending('codex')
    assert retry['id']==retry_id and retry['id']!=job_id
    assert retry['read_only'] and retry['reply_to']=='claude'
    assert 'Не редактируй файлы' in retry['prompt']
    assert 'обнови NIGHT_REPORT.md' not in retry['prompt']
