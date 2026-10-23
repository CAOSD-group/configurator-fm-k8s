import os

from dotenv import load_dotenv
from google import genai
from google.genai import types
from google.genai.errors import ServerError

load_dotenv()

model = os.getenv("GEMINI_MODEL", "gemini-3.8-flash")

system_prompt = """
You are an expert Kubernetes configuration engineer.
Your task is to generate correct and deployable Kubernetes manifests
from user-provided configuration requirements.
Return only the valid YAML manifest.
"""

user_prompt = """
Kubernetes version: 1.37

Create a Kubernetes Deployment that explicitly configures
its deployment strategy.
"""

client = genai.Client()

try:
    response = client.models.generate_content(
        model=model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            max_output_tokens=2048,
        ),
    )

except ServerError as exc:
    print("\n[GEMINI] API ERROR")
    print(exc)
    raise SystemExit(1)


print(response.text)