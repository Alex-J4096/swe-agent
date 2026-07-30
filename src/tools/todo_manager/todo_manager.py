from pydantic import BaseModel, Field

from src.runtime.session import Session
from src.tools.base import BaseTool, ToolContext

TODO_STATE_KEY = "todo_manager"


def get_todo_tasks(session: Session) -> list[dict[str, str]]:
    tasks = session.tool_state.get(TODO_STATE_KEY, [])
    return tasks if isinstance(tasks, list) else []


def has_unfinished_tasks(tasks: list[dict[str, str]]) -> bool:
    return any(task.get("status") != "completed" for task in tasks)


def render_todo_list(tasks: list[dict[str, str]]) -> str:
    if not tasks:
        return "No todo tasks."

    lines = []
    for task in tasks:
        marker = {
            "pending": "[ ]",
            "in_progress": "[>]",
            "completed": "[x]",
        }[task["status"]]
        lines.append(f"{marker} #{task['id']}: {task['text']}")

    done = sum(1 for task in tasks if task["status"] == "completed")
    lines.append(f"\n({done}/{len(tasks)} completed)")
    return "\n".join(lines)


class TodoManagerArgs(BaseModel):
    tasks: list = Field(
        description=(
            "Complete todo list to store. Each item must include text, and may "
            "include id and status (pending, in_progress, or completed)."
        )
    )


class TodoManager(BaseTool[TodoManagerArgs]):
    name = TODO_STATE_KEY
    description = (
        "Store the complete todo list for the current session. "
        "Always include every item and update statuses as work progresses. "
        "Mark an item completed immediately after finishing it."
    )
    args_model = TodoManagerArgs

    def validate(self, tasks: list) -> list[dict[str, str]]:
        if len(tasks) > 20:
            raise ValueError("Max 20 todos allowed")

        validated = []
        in_progress_count = 0
        for i, task in enumerate(tasks):
            task_id = str(task.get("id", i + 1))
            text = str(task.get("text", "").strip())
            status = str(task.get("status", "pending").lower())

            if not text:
                raise ValueError(f"Item {task_id}: text required")
            if status not in ("pending", "in_progress", "completed"):
                raise ValueError(f"Item {task_id}: invalid status '{status}'")
            if status == "in_progress":
                in_progress_count += 1
            validated.append({
                "id": task_id,
                "text": text,
                "status": status,
            })

        if in_progress_count > 1:
            raise ValueError("Only one task can be in_progress at a time.")

        return validated

    def run(self, args: TodoManagerArgs, context: ToolContext) -> dict:
        tasks = self.validate(args.tasks)
        context.session.tool_state[self.name] = tasks

        return {
            "ok": True,
            "content": render_todo_list(tasks),
        }
