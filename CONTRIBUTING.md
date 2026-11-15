# Contributing

Small, test-backed changes are welcome.

1. Create a branch from `main`.
2. Install `requirements-dev.txt` in a virtual environment.
3. Add a minimal failing receipt-text case when changing parser behavior.
4. Run `python -m pytest -q`.
5. Open a pull request describing the receipt format and expected behavior.

Do not commit real receipts, API keys, SQLite databases, or personal purchase
data. Prefer the synthetic receipt generator for examples and bug reproductions.
