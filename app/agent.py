import sqlite3
import threading
import time
from pathlib import Path
from typing import TypedDict
from uuid import uuid4

import httpx
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from app.config import Settings
from app.providers import Provider


class State(TypedDict, total=False):
    task_id: str
    goal: str
    query: str
    scenario: str
    status: str
    plan: dict
    step: int
    pages: list
    documents: list
    draft: str
    publication: dict
    trace: list
    tool_calls: int
    llm_calls: int
    input_tokens: int
    output_tokens: int
    usage_known: bool
    max_tool_calls: int
    retry_count: int
    repairs: int
    active_seconds: float
    error: str


def error_label(exc):
    if isinstance(exc, httpx.HTTPStatusError):
        return f'外部接口 HTTP {exc.response.status_code}'
    if isinstance(exc, httpx.TransportError):
        return '外部接口网络错误或超时'
    if isinstance(exc, ValueError):
        return str(exc)[:180]
    return f'执行异常：{type(exc).__name__}'


def transient(exc):
    return (isinstance(exc, (httpx.TransportError, TimeoutError)) or
            isinstance(exc, httpx.HTTPStatusError) and
            (exc.response.status_code == 429 or exc.response.status_code >= 500))


class Agent:
    """Single-process demo. Each graph step commits a durable checkpoint."""

    def __init__(self, settings: Settings):
        self.settings = settings
        Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.provider = Provider(settings)
        self.db = sqlite3.connect(settings.db_path, check_same_thread=False)
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, created REAL)')
        builder = StateGraph(State)
        for name in ('plan', 'execute', 'check', 'approval', 'publish'):
            builder.add_node(name, getattr(self, name))
        builder.add_edge(START, 'plan')
        builder.add_conditional_edges('plan', lambda s: END if s['status'] == 'failed' else 'execute')
        builder.add_conditional_edges('execute', self.after_execute)
        builder.add_conditional_edges('check', lambda s: END if s['status'] == 'failed'
                                      else 'execute' if s['status'] == 'repairing' else 'approval')
        builder.add_conditional_edges('approval', lambda s: 'publish' if s['status'] == 'approved' else END)
        builder.add_edge('publish', END)
        self.graph = builder.compile(checkpointer=SqliteSaver(self.db))

    def close(self):
        self.provider.close()
        self.db.close()

    @staticmethod
    def config(task_id):
        return {'configurable': {'thread_id': task_id}, 'recursion_limit': 64}

    @staticmethod
    def event(s, node, detail):
        return s.get('trace', []) + [{'node': node, 'detail': detail, 'at': round(time.time(), 3)}]

    def usage(self, s, usage):
        return {'llm_calls': s['llm_calls'] + int(self.settings.mode == 'live'),
                'input_tokens': s['input_tokens'] + usage.get('input_tokens', 0),
                'output_tokens': s['output_tokens'] + usage.get('output_tokens', 0),
                'usage_known': s['usage_known'] and usage.get('usage_known', True)}

    def plan(self, s):
        start = time.perf_counter()
        try:
            plan, usage = self.provider.plan(s['goal'], s['query'])
            return {'plan': plan, 'status': 'running', **self.usage(s, usage),
                    'active_seconds': s['active_seconds'] + time.perf_counter() - start,
                    'trace': self.event(s, 'plan', f"检索词：{plan['query']}；计划：search → read → generate")}
        except Exception as exc:
            return {'status': 'failed', 'error': error_label(exc),
                    'llm_calls': s['llm_calls'] + int(self.settings.mode == 'live'),
                    'usage_known': False, 'active_seconds': s['active_seconds'] + time.perf_counter() - start,
                    'trace': self.event(s, 'plan', '规划失败，已停止')}

    def execute(self, s):
        if s['tool_calls'] >= s['max_tool_calls']:
            return {'status': 'failed', 'error': '工具调用预算耗尽',
                    'trace': self.event(s, 'budget', '已停止后续调用')}
        tool = s['plan']['steps'][s['step']]
        start = time.perf_counter()
        result = {'tool_calls': s['tool_calls'] + 1}
        try:
            if tool == 'search':
                if s['scenario'] == 'transient_search' and s['retry_count'] == 0:
                    raise TimeoutError('演示注入：首次检索超时')
                result['pages'] = self.provider.search(s['plan']['query'])
                if not result['pages']:
                    raise ValueError('没有匹配资料，请更换标题检索词')
            elif tool == 'read':
                result['documents'] = self.provider.read(s['pages'])
                if not any(d['text'].strip() for d in result['documents']):
                    raise ValueError('资料正文为空，无法生成有据总结')
            elif tool == 'generate':
                draft, usage = self.provider.generate(s['goal'], s['documents'], bool(s['repairs']))
                result.update(self.usage(s, usage))
                result['draft'] = '' if s['scenario'] == 'empty_draft' and not s['repairs'] else draft
            else:
                raise ValueError('工具不在白名单')
            result.update(step=s['step'] + 1, retry_count=0, status='running',
                          trace=self.event(s, tool, '执行成功'))
        except Exception as exc:
            # At most one retry per step; each attempt consumes tool budget.
            retry = transient(exc) and s['retry_count'] < 1
            result.update(status='running' if retry else 'failed', retry_count=s['retry_count'] + 1,
                          error='' if retry else error_label(exc),
                          trace=self.event(s, tool, error_label(exc) + ('；重试一次' if retry else '；已停止')))
            if tool == 'generate' and self.settings.mode == 'live':
                result.update(llm_calls=s['llm_calls'] + 1, usage_known=False)
        result['active_seconds'] = s['active_seconds'] + time.perf_counter() - start
        return result

    @staticmethod
    def after_execute(s):
        if s['status'] == 'failed':
            return END
        return 'check' if s['step'] == len(s['plan']['steps']) else 'execute'

    def check(self, s):
        valid = len(s['draft']) >= 100 and all(f"[{d['id']}]" in s['draft'] for d in s['documents'])
        if valid:
            return {'status': 'awaiting_approval', 'trace': self.event(s, 'check', '长度和来源引用检查通过，等待确认发布')}
        if s['repairs'] < 1:
            return {'status': 'repairing', 'repairs': s['repairs'] + 1, 'step': 2,
                    'trace': self.event(s, 'check', '草稿缺少正文或引用，有限修正一次')}
        return {'status': 'failed', 'error': '修正后仍未通过长度和引用检查',
                'trace': self.event(s, 'check', '达到修正上限')}

    def approval(self, s):
        approved = interrupt({'message': '确认发布这份草稿？', 'title': s['plan']['title'],
                              'destination': '本地演示文档' if self.settings.mode == 'demo'
                              else self.settings.notion_parent_id})
        return {'status': 'approved' if approved is True else 'cancelled',
                'trace': self.event(s, 'approval', '用户确认' if approved is True else '用户取消')}

    def publish(self, s):
        if s['tool_calls'] >= s['max_tool_calls']:
            return {'status': 'failed', 'error': '发布前工具调用预算耗尽',
                    'trace': self.event(s, 'publish', '预算不足，未写入')}
        start = time.perf_counter()
        try:
            publication = self.provider.publish(s['task_id'], s['plan']['title'], s['draft'])
            result = {'status': 'completed', 'publication': publication,
                      'trace': self.event(s, 'publish', '已保存到' + publication['destination'])}
        except Exception as exc:
            result = {'status': 'failed', 'error': error_label(exc) + '；请检查目标页面，写入不自动重试',
                      'trace': self.event(s, 'publish', '发布失败或结果未知，已停止')}
        return {**result, 'tool_calls': s['tool_calls'] + 1,
                'active_seconds': s['active_seconds'] + time.perf_counter() - start}

    def create(self, goal, query='', max_tool_calls=6, scenario='normal'):
        with self.lock:
            task_id = str(uuid4())
            self.db.execute('INSERT INTO tasks VALUES (?,?)', (task_id, time.time()))
            self.db.commit()
            self.graph.invoke({'task_id': task_id, 'goal': goal, 'query': query,
                'scenario': scenario, 'status': 'running', 'step': 0, 'trace': [], 'draft': '',
                'tool_calls': 0, 'llm_calls': 0, 'input_tokens': 0, 'output_tokens': 0,
                'usage_known': True, 'max_tool_calls': max_tool_calls, 'retry_count': 0,
                'repairs': 0, 'active_seconds': 0.0, 'error': ''}, self.config(task_id))
            return self.get(task_id)

    def get(self, task_id):
        with self.lock:
            if not self.db.execute('SELECT 1 FROM tasks WHERE id=?', (task_id,)).fetchone():
                raise KeyError(task_id)
            state = dict(self.graph.get_state(self.config(task_id)).values)
            rates_set = self.settings.input_price > 0 and self.settings.output_price > 0
            state['mode'] = self.settings.mode
            state['estimated_cost_usd'] = (0.0 if self.settings.mode == 'demo' else
                round((state.get('input_tokens', 0) * self.settings.input_price +
                       state.get('output_tokens', 0) * self.settings.output_price) / 1_000_000, 8)
                if rates_set and state.get('usage_known') else None)
            return state

    def resume(self, task_id, approved):
        with self.lock:
            state = self.get(task_id)
            if state['status'] != 'awaiting_approval':
                raise ValueError('只有等待发布确认的任务可以恢复')
            self.graph.invoke(Command(resume=approved), self.config(task_id))
            return self.get(task_id)

    def list_tasks(self):
        with self.lock:
            return [self.get(row[0]) for row in self.db.execute('SELECT id FROM tasks ORDER BY created DESC LIMIT 30').fetchall()]
