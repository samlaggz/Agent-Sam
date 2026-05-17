from __future__ import annotations

import argparse
from pathlib import Path
from typing import Callable, Sequence
from urllib.parse import urlparse

import yaml

from scripts.common import PROJECT_ROOT


OutputFunc = Callable[[str], None]

ALLOWLIST_PATH = PROJECT_ROOT / "deployment" / "captcha_allowlist.yaml"
DEFAULT_ALLOWLIST = {
    "version": 1,
    "description": (
        "Authorized domains where Agent_Sam may run an approved CAPTCHA-solving flow. "
        "Unlisted domains must trigger a prompt and approval check before any solver step."
    ),
    "prompt_for_unlisted": True,
    "domains": ["127.0.0.1", "localhost", "www.magnific.com"],
}


def run(argv: Sequence[str] | None = None, *, output: OutputFunc = print) -> int:
    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.command == "list":
            _list_allowlist(output=output)
            return 0
        if args.command == "add":
            _add_domains(args.domains, output=output)
            return 0
        if args.command == "remove":
            _remove_domains(args.domains, output=output)
            return 0
    except ValueError as exc:
        output(str(exc))
        return 1

    parser.print_help()
    return 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Inspect and edit the authorized CAPTCHA target allowlist.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="Show the current authorized CAPTCHA target domains.")

    add_parser = subparsers.add_parser("add", help="Add one or more authorized domains or URLs.")
    add_parser.add_argument("domains", nargs="+", help="Domain names or URLs to add to the allowlist.")

    remove_parser = subparsers.add_parser("remove", help="Remove one or more domains or URLs from the allowlist.")
    remove_parser.add_argument("domains", nargs="+", help="Domain names or URLs to remove from the allowlist.")

    return parser


def load_allowlist(path: Path | None = None) -> dict[str, object]:
    resolved_path = path or ALLOWLIST_PATH
    if not resolved_path.exists():
        return _normalize_allowlist(DEFAULT_ALLOWLIST)

    payload = yaml.safe_load(resolved_path.read_text(encoding="utf-8")) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"CAPTCHA allowlist at {resolved_path} must contain a YAML mapping.")
    return _normalize_allowlist(payload)


def save_allowlist(payload: dict[str, object], path: Path | None = None) -> None:
    resolved_path = path or ALLOWLIST_PATH
    normalized = _normalize_allowlist(payload)
    resolved_path.parent.mkdir(parents=True, exist_ok=True)
    resolved_path.write_text(yaml.safe_dump(normalized, sort_keys=False), encoding="utf-8")


def normalize_domain(raw_value: str) -> str:
    text = raw_value.strip().lower()
    if not text:
        raise ValueError("Domains cannot be empty.")

    candidate = text if "://" in text else f"https://{text}"
    parsed = urlparse(candidate)
    host = (parsed.netloc or parsed.path).strip().strip("/")
    if not host or any(character in host for character in ("/", "?", "#", " ")):
        raise ValueError(f"Invalid domain or URL: {raw_value}")
    return host


def _normalize_allowlist(payload: dict[str, object]) -> dict[str, object]:
    domains_value = payload.get("domains", DEFAULT_ALLOWLIST["domains"])
    if not isinstance(domains_value, list):
        raise ValueError("CAPTCHA allowlist domains must be a list.")

    normalized_domains: list[str] = []
    seen: set[str] = set()
    for raw_domain in domains_value:
        normalized_domain = normalize_domain(str(raw_domain))
        if normalized_domain in seen:
            continue
        normalized_domains.append(normalized_domain)
        seen.add(normalized_domain)

    description = str(payload.get("description") or DEFAULT_ALLOWLIST["description"]).strip()
    if not description:
        description = DEFAULT_ALLOWLIST["description"]

    return {
        "version": 1,
        "description": description,
        "prompt_for_unlisted": bool(payload.get("prompt_for_unlisted", True)),
        "domains": normalized_domains,
    }


def _list_allowlist(*, output: OutputFunc) -> None:
    allowlist = load_allowlist()
    output(f"Allowlist file: {ALLOWLIST_PATH}")
    output(str(allowlist["description"]))
    output(f"Prompt for unlisted domains: {'yes' if allowlist['prompt_for_unlisted'] else 'no'}")
    output("Authorized CAPTCHA target domains:")
    for domain in allowlist["domains"]:
        output(f"- {domain}")


def _add_domains(domains: Sequence[str], *, output: OutputFunc) -> None:
    allowlist = load_allowlist()
    existing_domains = list(allowlist["domains"])
    added_domains: list[str] = []

    for raw_domain in domains:
        normalized_domain = normalize_domain(raw_domain)
        if normalized_domain in existing_domains:
            continue
        existing_domains.append(normalized_domain)
        added_domains.append(normalized_domain)

    allowlist["domains"] = existing_domains
    save_allowlist(allowlist)

    if not added_domains:
        output("No changes made. All provided domains were already allowlisted.")
        return

    output("Added authorized CAPTCHA target domains:")
    for domain in added_domains:
        output(f"- {domain}")


def _remove_domains(domains: Sequence[str], *, output: OutputFunc) -> None:
    allowlist = load_allowlist()
    existing_domains = list(allowlist["domains"])
    removed_domains: list[str] = []

    for raw_domain in domains:
        normalized_domain = normalize_domain(raw_domain)
        if normalized_domain not in existing_domains:
            continue
        existing_domains.remove(normalized_domain)
        removed_domains.append(normalized_domain)

    allowlist["domains"] = existing_domains
    save_allowlist(allowlist)

    if not removed_domains:
        output("No changes made. None of the provided domains were in the allowlist.")
        return

    output("Removed authorized CAPTCHA target domains:")
    for domain in removed_domains:
        output(f"- {domain}")


if __name__ == "__main__":
    raise SystemExit(run())