from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from llm_providers import create_provider


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_name(value: str, max_len: int = 120) -> str:
    value = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._")
    if len(value) <= max_len:
        return value
    digest = hashlib.sha1(value.encode("utf-8")).hexdigest()[:10]
    return f"{value[:max_len - 11]}_{digest}"


def load_json(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def load_text(path: Path) -> str:
    return path.read_text(encoding="utf-8").strip()


def select_cases(
    dataset: dict[str, Any],
    *,
    group: str,
    prompt_status: str,
    case_ids: set[str] | None,
    limit: int | None,
) -> list[dict[str, Any]]:
    cases = list(dataset.get("cases", []))

    selected: list[dict[str, Any]] = []
    for case in cases:
        if prompt_status != "ALL" and case.get("prompt_status") != prompt_status:
            continue

        if group != "ALL" and case.get("benchmark_group") != group:
            continue

        if case_ids and case.get("case_id") not in case_ids:
            continue

        selected.append(case)

    if limit is not None:
        selected = selected[:limit]

    return selected


def strict_yaml_summary(raw_output: str) -> dict[str, Any]:
    """Parse the RAW response only. Never strips Markdown or repairs output."""
    result = {
        "nonempty": bool(raw_output.strip()),
        "contains_markdown_fence": "```" in raw_output,
        "yaml_parse_ok": False,
        "yaml_document_count": None,
        "yaml_root_is_mapping": False,
        "yaml_error": None,
    }

    try:
        import yaml
    except ImportError:
        result["yaml_error"] = "PyYAML not installed; YAML check skipped."
        return result

    try:
        docs = list(yaml.safe_load_all(raw_output))
        result["yaml_parse_ok"] = True
        result["yaml_document_count"] = len(docs)
        result["yaml_root_is_mapping"] = (
            len(docs) == 1 and isinstance(docs[0], dict)
        )
    except Exception as exc:
        result["yaml_error"] = f"{type(exc).__name__}: {exc}"

    return result


def experiment_path(
    results_dir: Path,
    *,
    resource: str,
    provider: str,
    model: str,
    case_id: str,
    repetition: int,
) -> Path:
    return (
        results_dir
        / safe_name(resource.lower())
        / safe_name(provider.lower())
        / safe_name(model)
        / safe_name(case_id)
        / f"run_{repetition:03d}"
    )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def run_one(
    *,
    provider_obj: Any,
    provider_name: str,
    model: str,
    system_prompt: str,
    case: dict[str, Any],
    repetition: int,
    results_dir: Path,
    temperature: float,
    max_output_tokens: int,
    overwrite: bool,
) -> tuple[str, Path]:
    case_id = str(case["case_id"])
    resource = str(case["resource"])
    out_dir = experiment_path(
        results_dir,
        resource=resource,
        provider=provider_name,
        model=model,
        case_id=case_id,
        repetition=repetition,
    )
    result_path = out_dir / "result.json"

    if result_path.exists() and not overwrite:
        try:
            previous = load_json(result_path)
            if previous.get("generation_status") == "SUCCESS":
                return "SKIPPED_EXISTING", out_dir
        except Exception:
            pass

    out_dir.mkdir(parents=True, exist_ok=True)

    user_prompt = str(case["user_prompt"])

    request = {
        "case_id": case_id,
        "resource": resource,
        "api_version": case.get("api_version"),
        "kubernetes_version": case.get("kubernetes_version"),
        "benchmark_group": case.get("benchmark_group"),
        "classification": case.get("classification"),
        "config_path": case.get("config_path"),
        "point_feature": case.get("point_feature"),
        "variant_labels": case.get("variant_labels", []),
        "plan_type": case.get("plan_type"),
        "n_array_ancestors": case.get("n_array_ancestors"),
        "provider": provider_name,
        "model": model,
        "repetition": repetition,
        "temperature": temperature,
        "max_output_tokens": max_output_tokens,
        "started_at_utc": utc_now_iso(),
    }

    write_json(out_dir / "request.json", request)
    (out_dir / "system_prompt.txt").write_text(
        system_prompt + "\n", encoding="utf-8"
    )
    (out_dir / "user_prompt.txt").write_text(
        user_prompt + "\n", encoding="utf-8"
    )

    try:
        generated = provider_obj.generate(
            model=model,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )

        # The raw model output is the immutable experimental artifact.
        (out_dir / "raw_output.txt").write_text(
            generated.text, encoding="utf-8"
        )

        if generated.thinking:
            (out_dir / "thinking.txt").write_text(
                generated.thinking, encoding="utf-8"
            )

        result = {
            **request,
            "finished_at_utc": utc_now_iso(),
            "generation_status": "SUCCESS",
            "latency_seconds": generated.latency_seconds,
            "usage": generated.usage,
            "provider_metadata": generated.provider_metadata,
            "strict_output_checks": strict_yaml_summary(generated.text),
            "error_type": None,
            "error_message": None,
        }
        write_json(result_path, result)
        return "SUCCESS", out_dir

    except KeyboardInterrupt:
        raise
    except Exception as exc:
        result = {
            **request,
            "finished_at_utc": utc_now_iso(),
            "generation_status": "ERROR",
            "latency_seconds": None,
            "usage": {},
            "provider_metadata": {},
            "strict_output_checks": None,
            "error_type": type(exc).__name__,
            "error_message": str(exc),
        }
        write_json(result_path, result)
        return "ERROR", out_dir


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run Kubernetes manifest-generation cases against an LLM provider. "
            "The runner preserves the raw model output and never repairs it."
        )
    )
    parser.add_argument("cases_json", type=Path)
    parser.add_argument(
        "--system-prompt",
        type=Path,
        required=True,
        help="Fixed system prompt used for every selected case.",
    )
    parser.add_argument(
        "--provider",
        choices=("ollama", "gemini"),
        required=True,
    )
    parser.add_argument(
        "--model",
        default=None,
        help=(
            "Model name. If omitted, OLLAMA_MODEL or GEMINI_MODEL is read "
            "from .env depending on provider."
        ),
    )
    parser.add_argument(
        "--group",
        choices=("PRIMARY", "EDGE", "ALL"),
        default="PRIMARY",
    )
    parser.add_argument(
        "--prompt-status",
        choices=("READY", "REVIEW", "SKIP", "ALL"),
        default="READY",
    )
    parser.add_argument(
        "--case-id",
        action="append",
        default=None,
        help="Run only this case_id. May be supplied multiple times.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-output-tokens", type=int, default=4096)
    parser.add_argument("--num-ctx", type=int, default=4096)
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--delay-seconds", type=float, default=0.0)
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path("results"),
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        default=None,
        help="Optional .env path. Defaults to project-root/.env.",
    )
    parser.add_argument(
        "--overwrite",
        action="store_true",
        help="Regenerate successful existing runs.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show selected cases without calling the provider.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    if args.repetitions < 1:
        raise SystemExit("--repetitions must be >= 1")

    script_root = Path(__file__).resolve().parents[1]
    env_file = args.env_file or (script_root / ".env")
    load_dotenv(env_file)

    dataset = load_json(args.cases_json)
    system_prompt = load_text(args.system_prompt)

    expected_format = dataset.get("format")
    if expected_format not in {
        "k8s_llm_prompt_cases_v2",
        "k8s_llm_prompt_cases_v1",
        None,
    }:
        print(
            f"Warning: unexpected dataset format: {expected_format}",
            file=sys.stderr,
        )

    case_ids = set(args.case_id) if args.case_id else None
    selected = select_cases(
        dataset,
        group=args.group,
        prompt_status=args.prompt_status,
        case_ids=case_ids,
        limit=args.limit,
    )

    print(f"Cases file: {args.cases_json}")
    print(f"Resource: {dataset.get('gvk', {}).get('kind', 'unknown')}")
    print(f"Selected cases: {len(selected)}")
    print(f"Group: {args.group}")
    print(f"Prompt status: {args.prompt_status}")
    print(f"Repetitions: {args.repetitions}")

    if not selected:
        print("No cases selected.")
        return 0

    if args.dry_run:
        for case in selected:
            print(
                f"- {case['case_id']} | "
                f"{case.get('benchmark_group')} | "
                f"{case.get('config_path')}"
            )
        return 0

    if args.provider == "ollama":
        model = args.model or os.getenv("OLLAMA_MODEL")
        if not model:
            raise SystemExit(
                "No model selected. Use --model or define OLLAMA_MODEL in .env."
            )
        ollama_host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
        gemini_api_key = None
    else:
        model = args.model or os.getenv("GEMINI_MODEL")
        if not model:
            raise SystemExit(
                "No model selected. Use --model or define GEMINI_MODEL in .env."
            )
        ollama_host = os.getenv("OLLAMA_HOST", "http://127.0.0.1:11434")
        gemini_api_key = os.getenv("GEMINI_API_KEY")

    provider_obj = create_provider(
        args.provider,
        ollama_host=ollama_host,
        timeout=args.timeout,
        num_ctx=args.num_ctx,
        gemini_api_key=gemini_api_key,
    )

    total_runs = len(selected) * args.repetitions
    counters = {
        "SUCCESS": 0,
        "ERROR": 0,
        "SKIPPED_EXISTING": 0,
    }
    current = 0

    for case in selected:
        for repetition in range(1, args.repetitions + 1):
            current += 1
            print(
                f"[{current}/{total_runs}] "
                f"{case['case_id']} run={repetition:03d} ... ",
                end="",
                flush=True,
            )

            status, out_dir = run_one(
                provider_obj=provider_obj,
                provider_name=args.provider,
                model=model,
                system_prompt=system_prompt,
                case=case,
                repetition=repetition,
                results_dir=args.results_dir,
                temperature=args.temperature,
                max_output_tokens=args.max_output_tokens,
                overwrite=args.overwrite,
            )
            counters[status] += 1
            print(f"{status} -> {out_dir}")

            if args.delay_seconds > 0 and current < total_runs:
                time.sleep(args.delay_seconds)

    print("\nSummary")
    for key, value in counters.items():
        print(f"  {key}: {value}")

    return 1 if counters["ERROR"] else 0


if __name__ == "__main__":
    raise SystemExit(main())