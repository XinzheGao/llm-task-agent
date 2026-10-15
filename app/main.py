from contextlib import asynccontextmanager
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from app.agent import Agent
from app.config import ROOT, Settings


class TaskRequest(BaseModel):
    goal: str = Field(min_length=2, max_length=2000)
    query: str = Field(default='', max_length=100)
    max_tool_calls: int = Field(default=6, ge=1, le=12)
    scenario: Literal['normal', 'transient_search', 'empty_draft'] = 'normal'


class ResumeRequest(BaseModel):
    approved: bool


def create_app(settings=None):
    @asynccontextmanager
    async def lifespan(app):
        app.state.agent = Agent(settings or Settings.from_env())
        yield
        app.state.agent.close()

    app = FastAPI(title='LLM Task Agent', lifespan=lifespan)

    @app.get('/')
    def index():
        return FileResponse(ROOT / 'app' / 'static' / 'index.html')

    @app.get('/api/health')
    def health():
        return {'status': 'ok', 'mode': app.state.agent.settings.mode}

    @app.get('/api/tasks')
    def tasks():
        return app.state.agent.list_tasks()

    @app.post('/api/tasks', status_code=201)
    def create(request: TaskRequest):
        if app.state.agent.settings.mode == 'live' and request.scenario != 'normal':
            raise HTTPException(400, '故障注入仅用于离线演示')
        return app.state.agent.create(**request.model_dump())

    @app.get('/api/tasks/{task_id}')
    def get(task_id: str):
        try:
            return app.state.agent.get(task_id)
        except KeyError:
            raise HTTPException(404, '任务不存在')

    @app.post('/api/tasks/{task_id}/resume')
    def resume(task_id: str, request: ResumeRequest):
        try:
            return app.state.agent.resume(task_id, request.approved)
        except KeyError:
            raise HTTPException(404, '任务不存在')
        except ValueError as exc:
            raise HTTPException(409, str(exc))

    return app


app = create_app()
