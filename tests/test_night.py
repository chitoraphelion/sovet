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
