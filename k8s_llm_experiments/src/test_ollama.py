from pathlib import Path
import os

from dotenv import load_dotenv
from ollama import Client


# ---------------------------------------------------------
# Project paths
# ---------------------------------------------------------

ROOT = Path(__file__).resolve().parents[1]

ENV_PATH = ROOT / ".env"
SYSTEM_PROMPT_PATH = ROOT / "prompts" / "system.txt"
CASE_PATH = ROOT / "cases" / "deployment_strategy.txt"

RESULTS_DIR = ROOT / "results" / "ollama" / "deployment_strategy"


# ---------------------------------------------------------
# Load environment
# ---------------------------------------------------------

load_dotenv(ENV_PATH)

model = os.getenv("OLLAMA_MODEL", "qwen3:8b")
host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")


# ---------------------------------------------------------
# Load prompts
# ---------------------------------------------------------

system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
user_prompt = CASE_PATH.read_text(encoding="utf-8")


# ---------------------------------------------------------
# Ollama
# ---------------------------------------------------------

print(f"Using model: {model}")
print(f"Ollama host: {host}")
print("Generating manifest...")

client = Client(
    host=host,
    timeout=120.0,
)
response = client.chat(
    model=model,
    messages=[
        {
            "role": "system",
            "content": system_prompt,
        },
        {
            "role": "user",
            "content": user_prompt,
        },
    ],
    options={
        "temperature": 0,
        "num_ctx": 4096,
    },
)

raw_output = response.message.content


# ---------------------------------------------------------
# Save raw output
# ---------------------------------------------------------

RESULTS_DIR.mkdir(parents=True, exist_ok=True)

raw_path = RESULTS_DIR / "raw_output.yaml"
raw_path.write_text(raw_output, encoding="utf-8")

print("\n--- OLLAMA RAW OUTPUT ---")
print(raw_output)

print(f"\nRaw output saved to:\n{raw_path}")