"""Offline workflow checks, not an LLM capability benchmark. Run as a module."""
import json
import platform
import tempfile
import time
from datetime import datetime, timezone
from importlib.metadata import version
from pathlib import Path

from app.agent import Agent
from app.config import ROOT, Settings


def main():
    cases = json.loads((ROOT / 'evaluation' / 'cases.json').read_text())
    rows = []
    with tempfile.TemporaryDirectory() as folder:
        agent = Agent(Settings(db_path=str(Path(folder) / 'eval.sqlite')))
        try:
            for case in cases:
                start = time.perf_counter()
                result = agent.create('整理 Agent 开发笔记', query=case['query'],
                    max_tool_calls=case['budget'], scenario=case['scenario'])
                if result['status'] == 'awaiting_approval':
                    result = agent.resume(result['task_id'], True)
                rows.append({'name': case['name'], 'expected': case['expected'],
                    'actual': result['status'], 'passed': result['status'] == case['expected'],
                    'seconds': round(time.perf_counter() - start, 6),
                    'tool_calls': result['tool_calls'], 'llm_calls': result['llm_calls'],
                    'tokens': result['input_tokens'] + result['output_tokens'],
                    'estimated_cost_usd': result['estimated_cost_usd'], 'error': result['error']})
        finally:
            agent.close()
    report = {'generated_at': datetime.now(timezone.utc).isoformat(), 'mode': 'demo',
        'python': platform.python_version(), 'langgraph': version('langgraph'),
        'note': '离线固定资料与模板；不衡量真实模型规划、生成质量或 Notion 可用性。耗时仅为本次本机记录。',
        'scenario_pass_rate': sum(r['passed'] for r in rows) / len(rows),
        'positive_task_completion_rate': sum(r['actual'] == 'completed' for r in rows if r['expected'] == 'completed') /
            sum(r['expected'] == 'completed' for r in rows), 'cases': rows}
    path = ROOT / 'evaluation' / 'results.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if not all(r['passed'] for r in rows):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
