"""System prompt fragment for working memory discipline.

Append this to any subagent's system prompt when working memory is enabled.
It instructs the agent to offload tool results to the memory store and keep
only summaries in conversation context.
"""

WORKING_MEMORY_PROMPT = """
## Working Memory

You have access to a working memory store via MCP tools. This is critical
for keeping your context small and costs low.

**Rules:**
1. After every tool call that returns >500 tokens of content, immediately
   call `memory_store` with the full result and a one-line summary.
2. In your response, reference only the summary — never paste full tool
   results into your message.
3. When you need prior findings, call `memory_recall` with a search query
   instead of scrolling back through conversation history.
4. Periodically call `memory_list` to see your index of stored entries.

**Example flow:**
- You call sourcegraph search, get 5K tokens of results
- Call `memory_store(key="sg-temporal-repos", content=<full results>, summary="Found 15 repos using Temporal SDK")`
- In your response, write: "Found 15 repos using Temporal SDK (stored as sg-temporal-repos)"
- Later, if you need those results: `memory_recall(query="temporal repos")`

**Other agents in this workflow can see your stored entries.** Write clear
summaries so they can find and use your findings without re-doing your work.
""".strip()


def with_working_memory(system_prompt: str) -> str:
    """Append working memory instructions to a system prompt."""
    return f"{system_prompt}\n\n{WORKING_MEMORY_PROMPT}"
