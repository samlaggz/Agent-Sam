from __future__ import annotations

import argparse

from app.config import get_settings
from harness.source_cache import SourceCacheService


def run() -> None:
    parser = argparse.ArgumentParser(description="Manage the Agent_Sam offline source cache.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    fetch_parser = subparsers.add_parser("fetch", help="Fetch and cache package source.")
    fetch_parser.add_argument("package_spec")

    path_parser = subparsers.add_parser("path", help="Print the cached path for a package.")
    path_parser.add_argument("package_spec")

    search_parser = subparsers.add_parser("search", help="Search the cached source for a package.")
    search_parser.add_argument("package_spec")
    search_parser.add_argument("query")

    arguments = parser.parse_args()
    service = SourceCacheService(get_settings())
    if arguments.command == "fetch":
        print(service.fetch(arguments.package_spec))
        return
    if arguments.command == "path":
        print(service.path(arguments.package_spec))
        return
    if arguments.command == "search":
        for match in service.search(arguments.package_spec, arguments.query):
            print(f"{match.path}:{match.line}:{match.text}")


if __name__ == "__main__":
    run()