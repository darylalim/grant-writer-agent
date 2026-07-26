"""Test environment setup.

conftest is imported before any test module, so setting a placeholder key here
lets the tests construct chat models without a real credential. No test makes a
network call.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-for-tests")
# Keep runs off the network and out of the user's LangSmith project.
os.environ["LANGSMITH_TRACING"] = "false"

# Blanked rather than popped. config.py calls load_dotenv() at import, which
# only skips keys already present in os.environ -- so popping this handed the
# real key straight back from a developer's .env, and the suite behaved one way
# locally and another on a clean checkout. Every consumer tests it with
# `if not os.getenv(...)`, so an empty string reads as absent, and being present
# is what stops load_dotenv overwriting it. Tests that want a key set it
# themselves with monkeypatch.
os.environ["TAVILY_API_KEY"] = ""
