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
        response = client.chat.completions.create(
            model=MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                *messages
            ],
            tools = TOOLS,
            tool_choice="auto",
            max_tokens=MAX_TOKENS,
        )

        assistant_message = response.choices[0].message
        messages.append({
            "role": "assistant",
            "content": assistant_message.content,
            "tool_calls": assistant_message.tool_calls,
        })

        if not assistant_message.tool_calls:
            print(assistant_message.content)
            return assistant_message.content

        for tool_call in assistant_message.tool_calls:
            tool_name = tool_call.function.name
            tool_args = tool_call.function.arguments

            print(f"Tool used: {tool_name}")
            print(f"Tool args: {tool_args}")

            output = "Test output"

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call.id,
                "content": output,
            })


if __name__ == "__main__":
    history = []
    while True:
        try:
            query = prompt("> ")
        except (EOFError, KeyboardInterrupt):
            break

        history.append({"role": "user", "content": query})
        agent_loop(history)
        response = history[-1]["content"]
        print(f"Response: {response}")