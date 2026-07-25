from pydantic import BaseModel, Field

from src.tools.base import BaseTool


class TodoManagerArgs(BaseModel):
    tasks: list = Field(
        description=(
            "Complete todo list to store. Each item must include text, and may "
            "include id and status (pending, in_progress, or completed)."
        )
    )


class TodoManager(BaseTool[TodoManagerArgs]):
    name = "todo_manager"
    description = "Update task list. Track progress on multi-step tasks."
    args_model = TodoManagerArgs

    def __init__(self):
        # td清单
        self.tasks_list = []

    def update(self, tasks: list) -> str:
        if len(tasks) > 20:
            raise ValueError("Max 20 todos allowed")
        # 校验任务的合法性
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

        self.tasks_list = validated
        return self.redner()

    def run(self, args: TodoManagerArgs) -> dict:
        content = self.update(args.tasks)
        print(content)
        return {
            "ok": True,
            "content": content,
        }

    # 打印 td list
    def redner(self) -> str:
        if not self.tasks_list:
            return "No todo tasks."

        lines = []
        for task in self.tasks_list:
            marker = {
                "pending": "[ ]",
                "in_progress": "[>]",
                "completed": "[x]",
            }[task["status"]]
            lines.append(f"{marker} #{task['id']}: {task['text']}")
        done = sum(1 for t in self.tasks_list if t["status"] == "completed")
        lines.append(f"\n({done}/{len(self.tasks_list)} completed)")

        return "\n".join(lines)
