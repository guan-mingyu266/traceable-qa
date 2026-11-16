.RECIPEPREFIX = >
.DEFAULT_GOAL := help
.PHONY: help install install-runtime pc-image up down logs test fmt probe bench demo

POWERCONTEXT_DIR ?= ../powercontext_competition
POWERCONTEXT_VERSION ?= 0.0.0.dev0

help:
> @grep -E '^[a-z-]+:' $(MAKEFILE_LIST) | sed 's/:.*//' | sort | column -c 80 || true

install:
> uv sync

install-runtime:
> uv sync --extra runtime

# 从 fork 构建运行时镜像（需要同级的 powercontext 检出）
pc-image:
> cd $(POWERCONTEXT_DIR) && docker build \
>   --file docker/Dockerfile \
>   --build-arg POWERCONTEXT_VERSION=$(POWERCONTEXT_VERSION) \
>   --tag powercontext-server:local .

up:
> docker compose up -d --build

down:
> docker compose down

logs:
> docker compose logs -f --tail=100

test:
> uv run pytest -q

fmt:
> uv run ruff check --fix src tests && uv run ruff format src tests

probe:
> @echo "P3: ingest + retrieve + pack, then print Recall@10 and bytes per question"

bench:
> @echo "P6: run eval/bench.py and write bench.json"

demo:
> @echo "P7: upload -> ask -> jump to the cited page"
