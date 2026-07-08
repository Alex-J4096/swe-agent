import json
import os
from prompt_toolkit import print_formatted_text as print
from prompt_toolkit import prompt
from src.infrastructure.model_provider import Provider

MAX_TOKENS = 8000
MODEL = "deepseek-ai/DeepSeek-V4-Flash"
SYSTEM_PROMPT = f"You are a coding assistant at {os.getcwd()}. You will be provided with a task description and you will generate code to complete the task. You will not provide any explanations or comments, only the code."
TOOLS = []

api_key=os.getenv("SILICONFLOW_API_KEY")
provider = Provider(provider_name="SiliconFlow", api_key=api_key)
client = provider._initialize_client()

def agent_loop(messages: list):
    while True:
        response = client.responses.create(
            model=MODEL,
            input=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *messages
            ],
            tools = TOOLS,
            max_output_tokens=MAX_TOKENS,
        )
        # 模型本轮输出放回历史中，历史包含 message / function_call 等 output_item
        messages.extend(response.output)

        tool_results = []

        for item in response.output:
            if item.type == "function_call":
                print(f"Tool used: {item.name} {item.arguments}")
                args = json.loads(item.arguments)

                # TODO: 根据工具名称调用对应的工具函数，并将结果添加到 tool_results 中
                # output = run_bash(args["command"])
                output = "test output"
                print(f"Tool output: {output[:200]}")

                tool_results.append({
                    "type": "function_call_output",
                    "call_id": item.call_id,
                    "output": output
                })

            # 没有工具调用，说明模型已经给出最终答案
            if not tool_results:
                return response.output_text

            # 如果有工具调用，但工具调用有结果，也将结果添加到消息中，供模型下一轮使用
            messages.extend(tool_results)





if __name__ == "__main__":
    history = []
    while True:
        try:
            query = prompt(">")
        except (EOFError, KeyboardInterrupt):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)
        response = history[-1]["content"]
        print(f"Response: {response}")