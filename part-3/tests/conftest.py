"""Pytest configuration and fixtures for the test suite."""
import os

# Set a dummy GROQ_API_KEY for tests. The groq_relevance module instantiates
# the Groq client at import time, which requires an API key to be set. Tests
# mock the actual API calls, so this dummy key is sufficient.
if not os.environ.get("GROQ_API_KEY"):
    os.environ["GROQ_API_KEY"] = "test-key-placeholder-not-used"
