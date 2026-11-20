RUN = docker compose run --rm app

.PHONY: up down reset test lint fmt seed demo demo-live logs loadtest

up:     ## build and start db + api on :8000
	docker compose up -d --build --wait

down:   ## stop everything and drop the database volume
	docker compose down -v

reset: down up  ## recreate the database from db/schema.sql

test:
	$(RUN) pytest

lint:
	$(RUN) sh -c "ruff check . && ruff format --check . && mypy"

fmt:
	$(RUN) sh -c "ruff format . && ruff check --fix ."

seed:
	$(RUN) python scripts/seed.py

demo: seed
	$(RUN) python scripts/demo.py

logs:
	docker compose logs -f app

demo-live: seed  ## same demo, pauses for Enter between steps (for recording)
	$(RUN) python scripts/demo.py --pause

loadtest:  ## latency/throughput, one hot company vs 16 companies (wipes demo data)
	$(RUN) python scripts/loadtest.py
