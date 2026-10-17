import concurrent.futures
import json
from unittest.mock import patch

import pytest

from sovet.core import Project, digest
from sovet.parallel import ensure_job, finish, final_phase, lane, working_dir
from sovet.native import native_command, prepare_native, instructions
from sovet.bridge import hook, inbox, complete_job
from sovet.mcp import available_tools, call
from sovet.night import NightRun


@pytest.fixture
def project(tmp_path):
    p=Project.init(tmp_path)
    p.config.update(profile='math-research',research_mode='parallel',research_run='test',research_steps=2,research_hours=1)
    p.save_config()
    for role in ('codex','claude'):
        folder=working_dir(p,role)
        (folder/'TASK.md').write_text('Find a new method and a proved bound, or a counterexample.')
    return p


def test_both_native_harnesses_are_authors_with_initial_task_prompt(project):
    with patch('sovet.native.shutil.which',side_effect=lambda r:'/bin/'+r):
        for role in ('codex','claude'):
            args=native_command(project,role)
            prepared=prepare_native(project,role)
            assert '--read-only' not in prepared['mcp']['args']
            assert 'план' not in args
            assert '-p' not in args and 'exec' not in args
            assert 'inbox' in args[-1]
            if role=='codex':
                assert args[args.index('-s')+1]=='workspace-write'
                assert args[args.index('-C')+1]==str(working_dir(project,role))
            else:
                assert '--permission-mode' not in args
            assert 'BOTH agents are researchers' in instructions(project,role)


def test_scoped_author_writes_and_concurrent_files_preserve_conflict_checks(project):
    for role in ('codex','claude'):
        own=lane(project,role)['directory']+'/ideas.md'
        peer=lane(project,'claude' if role=='codex' else 'codex')['directory']+'/ideas.md'
        project.write_artifact(own,'hypothesis',digest(b''),actor=role)
        with pytest.raises(ValueError):project.write_artifact(peer,'bad',digest(b''),actor=role)
        with pytest.raises(ValueError):project.write_artifact('results/research_notes.md','bad',digest(b''),actor=role)
        with pytest.raises(ValueError):project.write_artifact(own,'stale',digest(b''),actor=role)
        with pytest.raises(ValueError):project.write_artifact(own,'bad',digest(b'hypothesis'),actor=role,read_only=True)
    outside=project.root/'escape';outside.symlink_to(project.root.parent,target_is_directory=True)
    with pytest.raises(ValueError):project.write_artifact('escape/x.md','bad',digest(b''),actor='codex')


def test_no_intermediate_peer_review_and_no_headless_night(project):
    assert 'ask_peer' not in {x['name'] for x in available_tools(project,native=True)}
    with pytest.raises(ValueError):call(project,'codex',0,False,'ask_peer',{'prompt':'review'},native=True)
    (project.root/'RESEARCH_BRIEF.md').write_text('brief')
    with pytest.raises(ValueError,match='нативных CLI'):NightRun(project,'RESEARCH_BRIEF.md')


def test_independent_inboxes_finish_steps_without_creating_peer_jobs(project):
    for role in ('codex','claude'):
        ensure_job(project,role)
        job=inbox(project,role)['job']
        assert not job['read_only'] and job['reply_to'] is None
        complete_job(project,role,'new lemma checkpoint')
        next_job=inbox(project,role)['job']
        assert next_job['role']==role and next_job['origin']=='parallel-research'
        complete_job(project,role,'second substantial step')
        assert inbox(project,role)['job'] is None
    assert not any(j['origin'] in ('peer-result','final-review') for j in project.jobs())


def test_final_cross_review_only_after_both_dossiers_and_exactly_once(project):
    for role in ('codex','claude'):
        path=lane(project,role)['directory']+'/FINAL.md'
        project.write_artifact(path,'Conjecture with derivation, scope and unresolved gap.',digest(b''),actor=role)
        finish(project,role,path,'Substantive result')
        if role=='codex':
            assert not final_phase(project) and project.jobs()==[]
    assert final_phase(project)
    assert {j['origin'] for j in project.jobs()}=={'final-review'}
    assert len(project.jobs())==2 and all(j['read_only'] for j in project.jobs())
    finish(project,'codex',lane(project,'codex')['directory']+'/FINAL.md','Updated dossier')
    assert len(project.jobs())==2
    review=project.claim('codex')
    project.finish(review,'One final criticism')
    receipt=project.claim('claude')
    # There is still Claude's own queued final review before its correction receipt.
    assert receipt['read_only']
    project.finish(receipt,'Final review')
    correction=project.claim('claude')
    assert correction['origin']=='peer-result' and not correction['read_only']


def test_same_role_concurrent_inbox_does_not_seed_duplicate_jobs(project):
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda _:ensure_job(project,'codex'),range(8)))
    assert len(project.jobs())==1


def test_task_dispatch_deadline_does_not_kill_the_native_harness(project):
    ensure_job(project,'codex')
    job=project.claim('codex');project.finish(job,'checkpoint')
    with patch('sovet.parallel.time.time',return_value=10**12):ensure_job(project,'codex')
    assert len(project.jobs())==1


def test_native_launcher_uses_lane_cwd_and_bootstrap_without_model_calls(project):
    import os
    import sys
    from sovet.native import run_native
    for role in ('codex','claude'):
        fake=project.root/('fake-'+role)
        fake.write_text('#!'+sys.executable+'\nimport json,os,sys\nopen("native_launch.json","w").write(json.dumps({"cwd":os.getcwd(),"args":sys.argv[1:]}))\n')
        fake.chmod(0o755)
        with patch('sovet.native.shutil.which',return_value=str(fake)), patch('sovet.native.register',return_value=None):
            assert run_native(project,role)==0
        result=json.loads((working_dir(project,role)/'native_launch.json').read_text())
        assert result['cwd']==str(working_dir(project,role))
        assert 'inbox' in result['args'][-1]
        assert '-p' not in result['args'] and 'exec' not in result['args']
    assert {j['role'] for j in project.jobs()}=={'codex','claude'}
    assert all(j['origin']=='parallel-research' for j in project.jobs())
