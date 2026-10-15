import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / '.env')


@dataclass
class Settings:
    mode: str = 'demo'
    db_path: str = str(ROOT / 'data' / 'agent.sqlite')
    llm_base_url: str = 'https://api.openai.com/v1'
    llm_api_key: str = ''
    llm_model: str = ''
    notion_token: str = ''
    notion_parent_id: str = ''
    input_price: float = 0.0
    output_price: float = 0.0

    @classmethod
    def from_env(cls):
        settings = cls(
            mode=os.getenv('AGENT_MODE', 'demo'),
            db_path=os.getenv('AGENT_DB', str(ROOT / 'data' / 'agent.sqlite')),
            llm_base_url=os.getenv('LLM_BASE_URL', 'https://api.openai.com/v1'),
            llm_api_key=os.getenv('LLM_API_KEY', ''),
            llm_model=os.getenv('LLM_MODEL', ''),
            notion_token=os.getenv('NOTION_TOKEN', ''),
            notion_parent_id=os.getenv('NOTION_PARENT_PAGE_ID', ''),
            input_price=float(os.getenv('INPUT_PRICE_PER_MILLION', '0')),
            output_price=float(os.getenv('OUTPUT_PRICE_PER_MILLION', '0')),
        )
        if settings.mode not in ('demo', 'live'):
            raise ValueError('AGENT_MODE 必须为 demo 或 live')
        if settings.mode == 'live' and not all((settings.llm_api_key, settings.llm_model,
                                               settings.notion_token, settings.notion_parent_id)):
            raise ValueError('live 模式需要完整的 LLM 和 Notion 环境变量')
        return settings
