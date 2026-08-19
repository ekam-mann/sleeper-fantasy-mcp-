"""Make the project importable no matter how pytest is invoked.

`python -m pytest` puts the working directory on `sys.path`; a bare `pytest`
does not. Without this, tests pass locally under the first form and fail in CI
under the second with `ModuleNotFoundError: No module named 'ff'` - which is
exactly what happened.

pytest does prepend the rootdir when a conftest.py sits here, but doing it
explicitly means the tests also run under any invocation, working directory, or
import mode.
"""

import pathlib
import sys

ROOT = str(pathlib.Path(__file__).resolve().parent)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
