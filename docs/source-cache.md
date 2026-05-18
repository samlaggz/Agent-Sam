# Source Cache

Agent_Sam uses `opensrc` as a read-only dependency source cache for offline lookup and API learning.

## Commands

```powershell
python -m scripts.source_cache fetch pypi:requests
python -m scripts.source_cache path pypi:requests
python -m scripts.source_cache search pypi:requests Session
```

## Rules

- the cache is for search and reference, not blind vendoring
- large copied code blocks still require license care
- search runs through `rg` inside the cached source tree
- cache locations are controlled by `SOURCE_CACHE_DIR`

## Required settings

```env
SOURCE_CACHE_ENABLED=true
SOURCE_CACHE_DIR=./source-cache
OPENSRC_COMMAND=opensrc
RIPGREP_COMMAND=rg
```