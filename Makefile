.PHONY: test check compile selftest dry-run

test:            ## jalankan semua test (bridge + tickets + watcher)
	python3 -m unittest discover -s tests -v
	cd bridge && python3 -m unittest discover -s tests -v

check:            ## yang dijalankan sebelum commit: sintaks + seluruh test
	python3 -m compileall -q board bridge tickets tests watcher.py
	$(MAKE) test

compile:
	python3 -m py_compile tickets/store.py tickets/cli.py watcher.py bridge/t3_bridge.py

selftest:
	python3 bridge/t3_bridge.py --selftest
	python3 tickets/cli.py profiles

dry-run:
	python3 watcher.py --once --dry-run
