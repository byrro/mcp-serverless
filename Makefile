EXTERNAL_DIR := ./external
AWS_LAMBDA_SCRAPER := helpers/scrapers/aws-lambda-docs/scraper.py
ARCADE_DOCS_SCRAPER := helpers/scrapers/arcade-docs/scraper.py

.PHONY: build aws-lambda-docs arcade-docs test

# Run both scrapers in parallel with a two-line progress display.
# Each scraper's output is captured to a temp file; a polling loop reads
# the latest status from each file and renders it on a dedicated line
# using ANSI cursor movement.
build:
	@ad=$$(mktemp); al=$$(mktemp); \
	trap 'kill $$p1 $$p2 2>/dev/null; rm -f "$$ad" "$$al"; exit 130' INT TERM; \
	uv run $(ARCADE_DOCS_SCRAPER) >"$$ad" 2>&1 & p1=$$!; \
	uv run $(AWS_LAMBDA_SCRAPER) >"$$al" 2>&1 & p2=$$!; \
	printf 'Scraping documentation pages...\n\n\n'; \
	while kill -0 $$p1 2>/dev/null || kill -0 $$p2 2>/dev/null; do \
		printf '\033[2A'; \
		printf '  \033[36mArcade:\033[0m      %s\033[K\n' \
			"$$(tail -c 500 "$$ad" 2>/dev/null | LC_ALL=C tr '\r' '\n' | tail -1)"; \
		printf '  \033[33mAWS Lambda:\033[0m  %s\033[K\n' \
			"$$(tail -c 500 "$$al" 2>/dev/null | LC_ALL=C tr '\r' '\n' | tail -1)"; \
		sleep 0.5; \
	done; \
	wait $$p1; r1=$$?; wait $$p2; r2=$$?; \
	printf '\033[2A'; \
	printf '  \033[36mArcade:\033[0m      %s\033[K\n' \
		"$$(LC_ALL=C tr '\r' '\n' < "$$ad" | grep -E 'Done|BROKEN' | tail -1)"; \
	printf '  \033[33mAWS Lambda:\033[0m  %s\033[K\n' \
		"$$(LC_ALL=C tr '\r' '\n' < "$$al" | grep -E 'Done|BROKEN' | tail -1)"; \
	rm -f "$$ad" "$$al"; \
	[ $$r1 -eq 0 ] && [ $$r2 -eq 0 ]

arcade-docs:
	@echo "Scraping Arcade documentation..."
	@uv run $(ARCADE_DOCS_SCRAPER)

aws-lambda-docs:
	@echo "Scraping AWS Lambda documentation..."
	@uv run $(AWS_LAMBDA_SCRAPER)

test:
	cd helpers/scrapers/aws-lambda-docs && uv run pytest test_aws_lambda_scraper.py -v
	cd helpers/scrapers/arcade-docs && uv run pytest test_arcade_scraper.py -v
