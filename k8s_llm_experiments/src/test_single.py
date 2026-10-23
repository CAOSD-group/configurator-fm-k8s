from pathlib import Path
import os
import subprocess
import sys

import yaml
from dotenv import load_dotenv
from openai import OpenAI


ROOT = Path(__file__).parent

SYSTEM_PROMPT_PATH = ROOT / "prompts" / "system.txt"
CASE_PATH = ROOT / "cases" / "deployment_strategy.txt"

RESULTS_DIR = ROOT / "results" / "deployment_strategy"
RAW_OUTPUT_PATH = RESULTS_DIR / "raw_output.txt"
MANIFEST_PATH = RESULTS_DIR / "manifest.yaml"


def main():
    # ---------------------------------------------------------
    # 1. Configuration
    # ---------------------------------------------------------
    load_dotenv()

    api_key = os.getenv("OPENAI_API_KEY")
    model = os.getenv("OPENAI_MODEL")

    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not defined in .env")

    if not model:
        raise RuntimeError("OPENAI_MODEL is not defined in .env")

    system_prompt = SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
    user_prompt = CASE_PATH.read_text(encoding="utf-8")

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    # ---------------------------------------------------------
    # 2. LLM generation
    # ---------------------------------------------------------
    print(f"Generating manifest with model: {model}")

    client = OpenAI(api_key=api_key)

    response = client.responses.create(
        model=model,
        instructions=system_prompt,
        input=user_prompt,
    )

    raw_output = response.output_text

    # Save exactly what the LLM returned.
    RAW_OUTPUT_PATH.write_text(raw_output, encoding="utf-8")

    print("\n--- RAW OUTPUT ---")
    print(raw_output)

    # ---------------------------------------------------------
    # 3. YAML parsing
    # ---------------------------------------------------------
    try:
        manifest = yaml.safe_load(raw_output)
    except yaml.YAMLError as exc:
        print("\n[YAML] FAIL")
        print(exc)
        sys.exit(1)

    if not isinstance(manifest, dict):
        print("\n[YAML] FAIL")
        print("The YAML root is not a mapping/object.")
        sys.exit(1)

    print("\n[YAML] PASS")

    # We do NOT reserialize the parsed object.
    # We preserve the exact model output as the manifest.
    MANIFEST_PATH.write_text(raw_output, encoding="utf-8")

    print(f"Manifest saved to: {MANIFEST_PATH}")

    # ---------------------------------------------------------
    # 4. Kubernetes validation
    # ---------------------------------------------------------
    command = [
        "kubectl",
        "apply",
        "--dry-run=server",
        "--validate=strict",
        "-f",
        str(MANIFEST_PATH),
    ]

    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print("\n[KUBERNETES] SKIPPED")
        print("kubectl was not found in PATH.")
        return

    if result.returncode == 0:
        print("\n[KUBERNETES] PASS")
        print(result.stdout)
    else:
        print("\n[KUBERNETES] FAIL")
        print(result.stderr)


if __name__ == "__main__":
    main()