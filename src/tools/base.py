from typing import Any, Generic, TypeVar
from pydantic import BaseModel

# 定义类型变量，ArgsT必须继承BaseModel
ArgsT = TypeVar("ArgsT", bound=BaseModel)

class BaseTool(Generic[ArgsT]):
    name: str
    description: str
    args_model: type[ArgsT]

    @property
    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.args_model.model_json_schema(),
            },
        }

    def run(self, args: ArgsT) -> dict[str, Any]:
        raise NotImplementedError