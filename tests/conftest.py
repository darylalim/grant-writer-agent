"""Test environment setup.

conftest is imported before any test module, so setting a placeholder key here
lets the tests construct chat models without a real credential. No test makes a
network call.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-for-tests")
# Keep runs off the network and out of the user's LangSmith project.
os.environ["LANGSMITH_TRACING"] = "false"
os.environ.pop("TAVILY_API_KEY", None)
