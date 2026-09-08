"""Version policy shared by frozen execution contracts and prompt compilation."""

PROMPT_COMPILER_VERSION = 4
SUPPORTED_PROMPT_COMPILER_VERSIONS = frozenset({1, 2, 3, PROMPT_COMPILER_VERSION})
