from typing import List

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

PROVIDER_BASE_URLS = {
    "OpenAI": "https://api.openai.com/v1",
    "SiliconFlow": "https://api.siliconflow.cn/v1",
}

class Provider:
    def __init__(
        self,
        provider_name: str,
        api_key: str,
        base_url: str | None = None,
    ):
        self.provider_name = provider_name
        self.api_key = api_key
        self.base_url = base_url or PROVIDER_BASE_URLS.get(provider_name)
        self.client = self.initialize_client()

    def initialize_client(self):
        if self.base_url is None:
            return OpenAI(api_key=self.api_key)
        return OpenAI(base_url=self.base_url, api_key=self.api_key)

    def list_model_ids(self) -> List[str]:
        response = self.client.models.list()

        return sorted({
            model.id
            for model in response.data
            if model.id
        })

