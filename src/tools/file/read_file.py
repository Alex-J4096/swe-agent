from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field
from src.tools.base import BaseTool

class ReadFileArgs(BaseModel):
    file_path: str = Field(description="File path relative to project root.")

class ReadFileTool(BaseTool[ReadFileArgs]):
    pass