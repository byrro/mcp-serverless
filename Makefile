EXTERNAL_DIR := ./external
AWS_LAMBDA_SCRAPER := helpers/scrapers/aws-lambda-docs/scraper.py
ARCADE_DOCS_SCRAPER := helpers/scrapers/arcade-docs/scraper.py

.PHONY: build aws-lambda-docs arcade-docs test

build:
	$(MAKE) -j arcade-docs aws-lambda-docs

arcade-docs:
	@echo "Scraping Arcade documentation..."
	uv run $(ARCADE_DOCS_SCRAPER)

aws-lambda-docs:
	@echo "Scraping AWS Lambda documentation..."
	uv run $(AWS_LAMBDA_SCRAPER)

test:
	cd helpers/scrapers/aws-lambda-docs && uv run pytest test_aws_lambda_scraper.py -v
	cd helpers/scrapers/arcade-docs && uv run pytest test_arcade_scraper.py -v
