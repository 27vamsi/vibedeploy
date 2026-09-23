"""Entry point for the shim. Readme.md section 14.

This directory goes on PYTHONPATH in the deployed image, so Python imports this
module automatically at interpreter start, before any of the app's own code.
That is what makes the protection independent of the app cooperating.

Kept deliberately tiny and non-fatal: a crash here would take the app down.
"""

from __future__ import annotations

import os
import sys

if os.environ.get("VD_SHIM_DISABLED") != "1":
    try:
        import vibedeploy_shim

        vibedeploy_shim.install()
    except Exception as exc:  # pragma: no cover - defensive
        # Do not take the app down. The missing startup line is what tells the
        # worker this app is unprotected, and the deploy is blocked there.
        print(f"vibedeploy-shim failed to install: {exc!r}", file=sys.stderr, flush=True)
