"""HTTP contract tests use fake responses, no credentials or external writes."""
import json

import httpx

from app.agent import Agent
from app.config import Settings


def test_live_workflow_with_fake_http(tmp_path):
    agent = Agent(Settings(mode='live', db_path=str(tmp_path / 'live.sqlite'),
        llm_api_key='test-only', llm_model='test-model', notion_token='test-only',
        notion_parent_id='parent'))
    requests = []

    def handler(request):
        requests.append(request)
        payload = json.loads(request.content) if request.content else {}
        if request.url.path == '/v1/chat/completions':
            if 'tools' in payload:
                message = {'tool_calls': [{'function': {'name': 'plan_workflow', 'arguments': json.dumps({
                    'query': 'Agent', 'title': '总结', 'steps': ['search', 'read', 'generate']})}}]}
            else:
                message = {'content': '根据资料，' + '持久化保存任务状态。' * 20 + '[page1]'}
            return httpx.Response(200, json={'choices': [{'message': message}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 50}})
        if request.url.path == '/v1/search':
            return httpx.Response(200, json={'results': [{'id': 'page1', 'properties': {
                'title': {'type': 'title', 'title': [{'plain_text': 'Agent 状态管理'}]}}}]})
        if request.url.path == '/v1/blocks/page1/children':
            return httpx.Response(200, json={'results': [{'type': 'paragraph', 'paragraph': {
                'rich_text': [{'plain_text': '持久化保存任务状态。'}]}}], 'has_more': False})
        if request.url.path == '/v1/pages':
            assert payload['parent']['page_id'] == 'parent'
            return httpx.Response(200, json={'id': 'published1', 'url': 'https://notion.so/published1'})
        raise AssertionError(str(request.url))

    agent.provider.http.close()
    agent.provider.http = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        task = agent.create('总结 Agent 状态管理')
        assert task['status'] == 'awaiting_approval'
        assert not any(r.url.path == '/v1/pages' for r in requests)
        result = agent.resume(task['task_id'], True)
        assert result['status'] == 'completed'
        assert result['llm_calls'] == 2
        assert result['input_tokens'] == 200
        assert sum(r.url.path == '/v1/pages' for r in requests) == 1
    finally:
        agent.close()


def test_publish_timeout_not_retried(tmp_path, monkeypatch):
    agent = Agent(Settings(db_path=str(tmp_path / 'write.sqlite')))
    calls = []

    def uncertain(*args):
        calls.append(1)
        raise httpx.ReadTimeout('write outcome unknown')

    try:
        task = agent.create('整理 Agent 笔记')
        monkeypatch.setattr(agent.provider, 'publish', uncertain)
        failed = agent.resume(task['task_id'], True)
        assert failed['status'] == 'failed'
        assert len(calls) == 1
        assert '不自动重试' in failed['error']
    finally:
        agent.close()
