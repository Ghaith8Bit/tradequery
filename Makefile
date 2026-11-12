# Run from WSL, in a clone that lives in the WSL filesystem (~/tradequery), not /mnt/c.
export UID := $(shell id -u)
# Create the data folder as you, before Docker would create it as root (and then nothing could write to it).
$(shell mkdir -p data)
DC = docker compose
# --build rebuilds the image first (cached, so about a second when nothing changed);
# without it, code changes would be silently ignored by an old image.
RUN = $(DC) run --rm --build

.PHONY: test demo sample import show mt5-setup inspect sync calendar news ask eval eval-heldout eval-followups eval-smart backup web web-stop shell-mt5 clean-mt5

test:        ## unit tests, inside Docker
	$(RUN) --entrypoint pytest app -q

demo:        ## print a synthetic account (no MT5 needed)
	$(RUN) app demo

sample:      ## store the synthetic sample account (no MT5 needed), to try the page and questions
	$(RUN) app sample

import:      ## load an MT5 HTML report saved in data/: make import FILE=data/ReportHistory-<login>.html
	$(RUN) app import /data/$(notdir $(FILE))

show:        ## analyse the downloaded account
	$(RUN) app show

mt5-setup:   ## one-time: install Windows Python + MT5 into the Wine volume
	$(RUN) mt5 setup

inspect:     ## log in with the investor password; print account, symbols, history summary
	$(RUN) mt5 inspect

sync:        ## download the account history into ./data
	$(RUN) mt5 download

calendar:    ## export the MT5 economic calendar: make calendar FROM=2026.08.01 TO=2026.10.31 [SYMBOL=EURUSD]
	$(RUN) mt5 calendar $(FROM) $(TO) $(SYMBOL)

news:        ## export the calendar and join it to the trades: make news FROM=2026.08.01 TO=2026.10.31
	$(RUN) mt5 calendar $(FROM) $(TO) $(SYMBOL)
	$(RUN) app import-calendar

ask:         ## ask in plain English: make ask Q="which hour loses the most on gold?" (no Q: interactive)
	$(RUN) app ask $(if $(Q),"$(Q)",)

eval:        ## run the eval suite on Gemini (about 160 requests): make eval [ARGS="--limit 10"]
	$(RUN) -v $(CURDIR)/evals:/app/evals app eval $(ARGS)

# The sync worker (the page's "Sync now") needs the Wine volume from make mt5-setup.
MT5_READY = $(shell docker volume ls -q 2>/dev/null | grep -q 'mt5-wine$$' && echo yes)

eval-followups: ## follow-up questions asked after an earlier turn (about 10 requests)
	$(RUN) -v $(CURDIR)/evals:/app/evals app eval --questions evals/followups.jsonl --out evals/results/followups $(ARGS)

eval-smart:  ## normal vs smart answers on the same questions (about 80 requests)
	$(RUN) -v $(CURDIR)/evals:/app/evals app eval --smart $(ARGS)

eval-heldout: ## the held-out questions, never used to tune the prompt (about 50 requests)
	$(RUN) -v $(CURDIR)/evals:/app/evals app eval --questions evals/heldout.jsonl --out evals/results/heldout $(ARGS)

backup:      ## copy the account databases and news files to data/backups/ (make backup KEEP=20)
	$(RUN) app backup $(if $(KEEP),--keep $(KEEP),)

web:         ## start the web page in the background: http://localhost:8000 (stop: make web-stop)
	$(DC) up -d --build web $(if $(MT5_READY),sync-worker,)
	@echo "TradeQuery is running at http://localhost:8000"
	@$(if $(MT5_READY),echo "Sync now is available on the page (the MT5 sync worker is running).",echo "No MT5 set up: upload reports from the page, or run make mt5-setup for Sync now.")

web-stop:    ## stop the web page and the sync worker
	$(DC) stop web sync-worker

shell-mt5:   ## debug shell in the Wine container
	$(RUN) mt5 shell

clean-mt5:   ## forget the Wine volume (re-run mt5-setup afterwards)
	$(DC) down -v
