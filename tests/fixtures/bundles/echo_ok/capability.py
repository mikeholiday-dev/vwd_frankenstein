import os


def run(text: str) -> dict:
    return {"echo": text, "sees_api_key": "ANTHROPIC_API_KEY" in os.environ}
