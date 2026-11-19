"""Compatibility entrypoint for previously saved strategy prompts."""
from nerya.skills.builtin.prediction_market.scripts.inspect_prediction import run
from nerya.skills.manifest import cli_main

if __name__ == '__main__':
    cli_main(run)
