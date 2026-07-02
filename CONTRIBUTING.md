# Contributing to Halo Stats

Thank you for your interest in contributing! Here's how to help.

## Getting Started

1. **Fork** the repository
2. **Clone** your fork: `git clone https://github.com/yourusername/halostats.git`
3. **Create a branch**: `git checkout -b feature/your-feature`
4. **Make changes** and test thoroughly
5. **Submit a Pull Request**

## Development Setup

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Setup pre-commit hooks (optional)
pip install pre-commit
pre-commit install
```

## Code Style

- Follow [PEP 8](https://pep8.org/)
- Use 4 spaces for indentation
- Max line length: 100 characters
- Use type hints where possible

Format with Black:
```bash
black src/ tests/
```

## Testing

All PRs must include tests:

```bash
# Run tests
pytest tests/

# With coverage
pytest --cov=src tests/

# Specific test
pytest tests/test_webapp.py::test_function
```

## Commit Messages

Use clear, descriptive commit messages:

```
Bad: "fix bug"
Good: "Fix CSR calculation for tie outcomes"

Bad: "update"
Good: "Add leaderboard page with sorting"
```

## Pull Request Process

1. Update docs if needed
2. Add tests for new features
3. Ensure all tests pass
4. Update README.md if needed
5. Link any related issues
6. Request review from maintainers

## Areas for Contribution

- ✅ Bug fixes
- ✅ Performance improvements
- ✅ Documentation
- ✅ Tests
- ✅ New analytics features
- ✅ UI/UX improvements
- ✅ Docker/deployment improvements

## Reporting Issues

Include:
- Description of the bug
- Steps to reproduce
- Expected behavior
- Actual behavior
- Python version, OS, etc.

## Questions?

Open a GitHub Discussion or check existing issues first.

Thank you for contributing! 🎮
