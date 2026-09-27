# Contributing

1. Use Python 3.11 and create an isolated virtual environment.
2. Install `requirements.txt`; do not add unpinned direct dependencies.
3. Keep demo/dry-run defaults and live preflight fail-closed.
4. Do not include credentials, wallet addresses, account data, personal paths,
   private infrastructure identifiers, generated datasets, or trading logs.
5. Add focused `unittest` coverage for behavior changes.
6. Run the test, compile, shell, and privacy checks documented in `README.md`.

Pull requests should explain the safety impact and avoid claims of expected
returns. Changes that weaken an authentication, network-binding, execution, or
guardian gate require explicit maintainer review.
