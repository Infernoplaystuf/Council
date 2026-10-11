"""The local LLM: an OpenAI-compatible chat endpoint (llama-server or
Ollama's /v1) and strict JSON handling. Every caller must survive the LLM
being down — failures raise ``LLMError`` and the caller skips."""
