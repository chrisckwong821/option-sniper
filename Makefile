.PHONY: test test-python test-js run-mock

test: test-python test-js

test-python:
	python -m unittest discover -s tests -p 'test_*.py'

test-js:
	npm test

run-mock:
	python -m scripts.live_price_adapter --mock
