# Run from WSL, in a clone that lives in the WSL filesystem (~/tradequery), not /mnt/c.
export UID := $(shell id -u)
DC = docker compose

.PHONY: test demo show mt5-setup inspect sync shell-mt5 clean-mt5

test:        ## unit tests, inside Docker
	$(DC) run --rm --entrypoint pytest app -q

demo:        ## print a synthetic account (no MT5 needed)
	$(DC) run --rm app demo

show:        ## analyse the downloaded account
	$(DC) run --rm app show

mt5-setup:   ## one-time: install Windows Python + MT5 into the Wine volume
	$(DC) run --rm mt5 setup

inspect:     ## log in with the investor password; print account, symbols, history summary
	$(DC) run --rm mt5 inspect

sync:        ## download the account history into ./data
	$(DC) run --rm mt5 download

shell-mt5:   ## debug shell in the Wine container
	$(DC) run --rm mt5 shell

clean-mt5:   ## forget the Wine volume (re-run mt5-setup afterwards)
	$(DC) down -v
