
# 保留最近三个工具调用的结果
KEEP_RECENT = 3
PRESERVE_RESULT_TOOLS = {
    "read_file"
}
def estimate_tokens(messages: list) -> int:
    return len(str(messages)) // 4

def micro_compact(messages: list) -> list:
    tool_results = []
    for i, msg in enumerate(messages):
        # 查找工具调用的结果
        if msg["role"] == "tool" and isinstance(msg.get("content"), dict):
            tool_results.append((i, msg["content"]))

    if len(tool_results) <= KEEP_RECENT:
        return messages

    # 映射tool_call id和function.name，遍历之前的assistant消息，找到id和function.name的对应关系
    tool_name_map = {}
    for msg in messages:
        if msg["role"] == "assistant" and hasattr(msg, "tool_calls"):
            tool_calls_content = msg["tool_calls"]
            if isinstance(tool_calls_content, list):
                for block in tool_calls_content:
                    if hasattr(block, "id") and hasattr(block, "function"):
                        tool_name_map[block.id] = block["function"]["name"]

    # 清除工具调用的结果，除了白名单中的工具(read_file)
    to_clear = tool_results[:-KEEP_RECENT]
    for _, result in to_clear:
        if not isinstance(result.get("content"), str) or len(result["content"]) <= 100:
            continue
        tool_call_id = result.get("tool_call_id")
        tool_name = tool_name_map.get(tool_call_id, "Unknown")
        if tool_name in PRESERVE_RESULT_TOOLS:
            continue
        result["content"] = f"[Previous: used {tool_name}]"

    return messages

def auto_compact(messages: list) -> list:
    tool_results = []

    pass