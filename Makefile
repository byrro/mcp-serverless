EXTERNAL_DIR := ./external
ARCADE_DOCS_DIR := $(EXTERNAL_DIR)/arcade-docs
ARCADE_DOCS_REPO := git@github.com:ArcadeAI/docs.git

.PHONY: build

build: $(ARCADE_DOCS_DIR)

$(ARCADE_DOCS_DIR):
	@mkdir -p $(EXTERNAL_DIR)
	@echo "Cloning $(ARCADE_DOCS_REPO) into $(ARCADE_DOCS_DIR)..."
	git clone $(ARCADE_DOCS_REPO) $(ARCADE_DOCS_DIR)
