.PHONY: test test-python test-js coverage run-mock

test: test-python test-js

test-python:
	python -m unittest discover -s tests -p 'test_*.py'

test-js:
	npm test

coverage:
	python -m coverage erase
	python -m coverage run -m unittest discover -s tests -p 'test_*.py'
	python -m coverage report

run-mock:
	python -m scripts.live_price_adapter --mock
