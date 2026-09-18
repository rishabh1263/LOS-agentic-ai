import os


def ollama_host() -> str:
    return os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")


def ollama_model() -> str:
    return os.getenv("OLLAMA_MODEL", "qwen3:8b")
