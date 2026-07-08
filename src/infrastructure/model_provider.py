from openai import OpenAI
from dotenv import load_dotenv

load_dotenv()

base_url = {
    "OpenAI": "https://api.openai.com/v1",
    "SiliconFlow": "https://api.silicon.com/v1",
}

class Provider:
    def __init__(self, provider_name: str, api_key: str):
        self.provider_name = provider_name
        self.api_key = api_key
        self.client = self._initialize_client()

    def _initialize_client(self):
        if self.provider_name == "OpenAI":
            return OpenAI(api_key=self.api_key)
        elif self.provider_name == "SiliconFlow":
            return OpenAI(base_url=base_url["SiliconFlow"], api_key=self.api_key)




