"""Test environment setup.

conftest is imported before any test module, so setting a placeholder key here
lets the tests construct chat models without a real credential. No test makes a
network call.
"""

import os

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-dummy-for-tests")

# Keep runs off the network and out of the user's LangSmith project.
#
# All four spellings, because the switch has four and setting one leaves three
# live. `langsmith.utils.tracing_is_enabled` reads
# `get_env_var("TRACING_V2", default=get_env_var("TRACING", default=""))`, and
# `get_env_var` returns the first non-empty value across the namespaces
# ("LANGSMITH", "LANGCHAIN") -- so LANGCHAIN_TRACING_V2, the spelling LangChain's
# own docs emitted for years, outranks the LANGSMITH_TRACING this file used to
# set alone. See invariant 19; `test_review_fixes.py` section 11 pins it.
#
# The value must be "false". An empty string fails *open* -- `get_env_var` skips
# a value that strips to nothing and resumes the namespace fallback -- and "no"
# fails loud, since `env_var_is_set` excludes only {"", "0", "false", "False"}
# and `langchain_core.callbacks.manager` raises RuntimeError when
# LANGCHAIN_TRACING reads as set while v2 is off.
for _switch in (
    "LANGSMITH_TRACING_V2",
    "LANGCHAIN_TRACING_V2",
    "LANGSMITH_TRACING",
    "LANGCHAIN_TRACING",
):
    os.environ[_switch] = "false"

# The v1 tracer's other trigger, emptied for that same RuntimeError: forcing v2
# off above is what would newly arm it on a machine still exporting this.
os.environ["LANGCHAIN_HANDLER"] = ""

# Blanked rather than popped. config.py calls load_dotenv() at import, which
# only skips keys already present in os.environ -- so popping this handed the
# real key straight back from a developer's .env, and the suite behaved one way
# locally and another on a clean checkout. Every consumer tests it with
# `if not os.getenv(...)`, so an empty string reads as absent, and being present
# is what stops load_dotenv overwriting it. Tests that want a key set it
# themselves with monkeypatch.
os.environ["TAVILY_API_KEY"] = ""

# The LangSmith credential gets the same treatment for the same load_dotenv
# reason, and it is the half that does not depend on the list above being
# complete: those four names are a list upstream can extend, a credential is
# not. Without one, a switch that fails open costs nothing.
os.environ["LANGSMITH_API_KEY"] = ""
os.environ["LANGCHAIN_API_KEY"] = ""

# Non-empty on purpose: if a run ever does escape, it lands somewhere visibly
# wrong rather than in `default` or in the real `grant-writer`.
os.environ["LANGSMITH_PROJECT"] = "grant-writer-tests-should-be-empty"
