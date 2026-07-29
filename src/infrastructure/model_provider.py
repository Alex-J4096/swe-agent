from typing import List

from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

base_url = {
    "OpenAI": "https://api.openai.com/v1",
    "SiliconFlow": "https://api.siliconflow.cn/v1",
}

class Provider:
    def __init__(self, provider_name: str, api_key: str):
        self.provider_name = provider_name
        self.api_key = api_key
        self.client = self.initialize_client()

    def initialize_client(self):
        if self.provider_name == "OpenAI":
            return OpenAI(api_key=self.api_key)
        elif self.provider_name == "SiliconFlow":
            return OpenAI(base_url=base_url["SiliconFlow"], api_key=self.api_key)

        raise ValueError(f"Unknown provider: {self.provider_name}")

    def list_model_ids(self) -> List[str]:
        response = self.client.models.list()

        return sorted({
            model.id
            for model in response.data
            if model.id
        })


