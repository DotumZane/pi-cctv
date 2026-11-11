# Project workflow

After every code or configuration change:

1. Bump the application version in `cctv/__init__.py` and keep the package version in `pyproject.toml` aligned. Bump `__agent_version__` only when the Pi agent itself changes.
2. Run the relevant project checks.
3. Restart or reload the local live test server so it serves the changed code.
4. Commit the change.
5. Push the commit to the configured Git remote.

If the live server or Git push fails, report the failure clearly and keep working until the failure is resolved or an external service remains unavailable.
