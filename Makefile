# Run from WSL, in a clone that lives in the WSL filesystem (~/tradequery), not /mnt/c.
export UID := $(shell id -u)
# Create the data folder as you, before Docker would create it as root (and then nothing could write to it).
$(shell mkdir -p data)
DC = docker compose
# --build rebuilds the image first (cached, so about a second when nothing changed);
# without it, code changes would be silently ignored by an old image.
RUN = $(DC) run --rm --build

.PHONY: test demo sample import show mt5-setup inspect sync calendar news ask eval web web-stop shell-mt5 clean-mt5

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

web:         ## start the web page in the background: http://localhost:8000 (stop: make web-stop)
	$(DC) up -d --build web
	@echo "TradeQuery is running at http://localhost:8000"

web-stop:    ## stop the web page
	$(DC) stop web

shell-mt5:   ## debug shell in the Wine container
	$(RUN) mt5 shell

clean-mt5:   ## forget the Wine volume (re-run mt5-setup afterwards)
	$(DC) down -v
