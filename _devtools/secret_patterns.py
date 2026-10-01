"""Secret-detection patterns shared by CI and the test suite.

Deliberately dependency-free: this module is imported by
``.github/workflows/tests.yml`` inside a bare ``python -`` heredoc, where pytest
is not guaranteed to exist. An earlier version sourced the patterns from
``tests/test_secret_guard.py``, which imports pytest -- so the extraction
produced nothing, ``git grep -E ""`` matched every file, and the guard failed
every build while appearing to work.

Keeping the patterns here, with no imports, means both callers read the same
text and neither can drift.

Syntax notes, learned the hard way:

* ``[[:space:]]`` is required. ``\\s`` is a GNU extension and is unreliable in
  POSIX ERE across git versions; the original guard used it.
* The pattern is applied with ``git grep -i`` so ``TORBOX_API_KEY`` matches.
* The quote character is optional because ``.env`` values are frequently
  unquoted, and ``[\\"']?`` keeps both quoted forms.
"""

from __future__ import annotations

# 40+ characters: long enough that "your_key_here" and "${TORBOX_API_KEY}" do not
# trip it, short enough to catch a real key.
KEY_PATTERN = r"""(torbox[._]?)?api_?key[[:space:]]*=[[:space:]]*["']?[A-Za-z0-9_-]{40,}"""

# A Telegram session string is base64url and grants full account access.
SESSION_PATTERN = r"""session_string[[:space:]]*=[[:space:]]*["']?[A-Za-z0-9_-]{40,}"""

ALL_PATTERNS = (KEY_PATTERN, SESSION_PATTERN)