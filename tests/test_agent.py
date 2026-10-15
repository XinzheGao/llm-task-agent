import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agent import Agent
from app.config import Settings
from app.main import create_app


@pytest.fixture
def agent(tmp_path):
    service = Agent(Settings(db_path=str(tmp_path / 'test.sqlite')))
    yield service
    service.close()


def test_checkpoint_survives_restart_and_no_duplicate_publish(tmp_path):
    settings = Settings(db_path=str(tmp_path / 'restart.sqlite'))
    first = Agent(settings)
    task = first.create('整理 Agent 笔记')
    assert task['status'] == 'awaiting_approval'
    assert task['tool_calls'] == 3
    first.close()
    second = Agent(settings)
    try:
        assert second.get(task['task_id'])['draft'] == task['draft']
        done = second.resume(task['task_id'], True)
        assert done['status'] == 'completed'
        assert done['tool_calls'] == 4
        with pytest.raises(ValueError):
            second.resume(task['task_id'], True)
        with sqlite3.connect(settings.db_path) as db:
            assert db.execute('SELECT COUNT(*) FROM publications').fetchone()[0] == 1
    finally:
        second.close()


@pytest.mark.parametrize('scenario,expected_calls,repairs', [
    ('normal', 4, 0), ('transient_search', 5, 0), ('empty_draft', 5, 1)])
def test_success_and_repair(agent, scenario, expected_calls, repairs):
    task = agent.create('整理 Agent 笔记', scenario=scenario)
    done = agent.resume(task['task_id'], True)
    assert done['status'] == 'completed'
    assert done['tool_calls'] == expected_calls
    assert done['repairs'] == repairs
    assert done['llm_calls'] == 0


def test_budget_enforced_before_call(agent):
    task = agent.create('整理笔记', max_tool_calls=1)
    assert task['status'] == 'failed'
    assert task['tool_calls'] == 1
    assert '预算' in task['error']


def test_publish_budget_and_cancel(agent):
    task = agent.create('整理笔记', max_tool_calls=3)
    assert agent.resume(task['task_id'], True)['status'] == 'failed'
    task2 = agent.create('整理笔记')
    assert agent.resume(task2['task_id'], False)['status'] == 'cancelled'
    assert not agent.db.execute('SELECT * FROM publications').fetchall()


def test_no_results_does_not_invent_content(agent):
    task = agent.create('整理火星资料', query='不存在的主题')
    assert task['status'] == 'failed'
    assert task['draft'] == ''
    assert task['tool_calls'] == 1


def test_permanent_timeout_is_bounded(agent, monkeypatch):
    def timeout(*args):
        raise httpx.ReadTimeout('test')
    monkeypatch.setattr(agent.provider, 'search', timeout)
    task = agent.create('整理笔记')
    assert task['status'] == 'failed'
    assert task['tool_calls'] == 2


def test_bad_draft_is_only_repaired_once(agent, monkeypatch):
    monkeypatch.setattr(agent.provider, 'generate', lambda *a: ('bad', {}))
    task = agent.create('整理笔记')
    assert task['status'] == 'failed'
    assert task['repairs'] == 1
    assert task['tool_calls'] == 4


def test_api_contract(tmp_path):
    with TestClient(create_app(Settings(db_path=str(tmp_path / 'api.sqlite')))) as client:
        assert client.get('/').status_code == 200
        assert client.get('/api/tasks/missing').status_code == 404
        assert client.post('/api/tasks', json={'goal': 'x'}).status_code == 422
        response = client.post('/api/tasks', json={'goal': '整理 Agent 笔记'})
        assert response.status_code == 201
        task_id = response.json()['task_id']
        done = client.post(f'/api/tasks/{task_id}/resume', json={'approved': True})
        assert done.json()['status'] == 'completed'
        assert client.post(f'/api/tasks/{task_id}/resume', json={'approved': True}).status_code == 409
        assert len(client.get('/api/tasks').json()) == 1
