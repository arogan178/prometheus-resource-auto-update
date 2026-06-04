# Contributing to Prometheus Resource Auto-Update

First off, thank you for considering contributing to Prometheus Resource Auto-Update! It's people like you that make open source such a fantastic community.

## 1. Development Setup

This project uses standard Python tooling. 

1. **Clone the repo:**
   ```bash
   git clone https://github.com/your-org/goldi-oss.git
   cd goldi-oss
   ```

2. **Set up a virtual environment:**
   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   ```

3. **Install dependencies:**
   ```bash
   pip install -e ".[dev]"
   ```

## 2. Code Style and Linting

This project uses [Ruff](https://docs.astral.sh/ruff/) for fast linting and formatting, and [Mypy](https://mypy.readthedocs.io/) for static type checking.

Before submitting a pull request, please ensure your code passes all checks:

```bash
# Format your code
ruff format .

# Run the linter
ruff check .

# Check static typing
mypy .
```

## 3. Testing

We use `pytest` for unit testing. Please write tests for any new logic you add, especially around resource math and resource patch parsing.

To run the tests:
```bash
pytest tests/
```

## 4. Submitting a Pull Request

1. Fork the repository and create your branch from `main`.
2. If you've added code that should be tested, add tests.
3. If you've changed APIs, update the documentation.
4. Ensure the test suite passes.
5. Make sure your code lints (run `ruff` and `mypy`).
6. Issue that pull request!

## 5. Security Vulnerabilities

If you discover a security vulnerability, please DO NOT open an issue. Please email the maintainers directly.