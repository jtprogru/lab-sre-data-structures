PYTHON ?= python3
.DEFAULT_GOAL := help
.PHONY: help local docker all regex hashing bisect nginx postgres redis kernel
help: ## Показать команды
	@awk 'BEGIN {FS=":.*## "} /^[a-z_-]+:.*## / {printf "  %-14s %s\n", $$1, $$2}' $(MAKEFILE_LIST)
local: ## Регулярка, кольцо хеширования и git bisect без Docker
	$(PYTHON) scripts/lab.py local
docker: ## nginx, PostgreSQL, Redis и наблюдение conntrack в контейнере
	$(PYTHON) scripts/lab.py docker
all: ## Все замеры; результаты в results/*.json
	$(PYTHON) scripts/lab.py all
regex hashing bisect nginx postgres redis kernel:
	$(PYTHON) scripts/lab.py $@
