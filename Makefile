.PHONY: test compile check run

test:
	python3 -m unittest discover -s tests -v

compile:
	python3 -m compileall -q cctv

check: test compile

run:
	./start.sh --host 127.0.0.1 --port 8080
