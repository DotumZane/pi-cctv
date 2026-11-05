# Project workflow

After every code or configuration change:

1. Run the relevant project checks.
2. Restart or reload the local live test server so it serves the changed code.
3. Commit the change.
4. Push the commit to the configured Git remote.

If the live server or Git push fails, report the failure clearly and keep working until the failure is resolved or an external service remains unavailable.
