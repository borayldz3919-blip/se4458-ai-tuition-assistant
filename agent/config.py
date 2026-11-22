import os


class AgentSettings:
    def __init__(self) -> None:
        # ALL REST calls go through the API Gateway (never directly to the Tuition API)
        self.gateway_url = os.getenv("GATEWAY_URL", "http://localhost:8000").rstrip("/")
        self.mcp_url = os.getenv("MCP_URL", "http://localhost:8001/mcp")
        # Any OpenAI-compatible chat-completions endpoint. Default: Google Gemini (free tier).
        self.llm_base_url = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai/")
        self.llm_api_key = os.getenv("LLM_API_KEY", "")
        self.llm_model = os.getenv("LLM_MODEL", "gemini-3-flash-preview,gemini-3.5-flash-lite,gemini-flash-lite-latest,gemini-3.5-flash")
        self.max_tool_rounds = int(os.getenv("AGENT_MAX_TOOL_ROUNDS", "5"))
        self.trace_log = os.getenv("TRACE_LOG", "logs/trace.jsonl")
        self.timezone = os.getenv("APP_TIMEZONE", "Europe/Istanbul")


settings = AgentSettings()
