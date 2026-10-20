"""Console-script entry point for `ug` / `ucode`.

Serves a cached `ug auth-token` bearer before importing `ucode.cli`, whose
import graph (typer, rich, databricks.sdk) costs ~0.7s. Agents like pi run the
helper before every model request, so this keeps the common case in the tens of
milliseconds. Everything else is delegated to the full CLI unchanged.
"""

from __future__ import annotations

import sys


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "auth-token":
        from ucode import auth_token_log
        from ucode.token_cache import fast_path_token

        # Installed before the cache check so a caller's timeout is logged
        # whichever stage it lands in.
        auth_token_log.log_if_killed(sys.argv[2:])
        auth_token_log.set_stage("checking the token cache")
        token = fast_path_token(sys.argv[1:])
        if token:
            sys.stdout.write(token + "\n")
            return
        auth_token_log.set_stage("loading the ug CLI")

    from ucode.cli import main as cli_main

    cli_main()
