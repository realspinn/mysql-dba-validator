"""Production launcher for the local connector.

Run with:  python -m connector --allow-origin http://127.0.0.1:8420

Security model
--------------
* The connector binds to 127.0.0.1 only.
* Browser origins are explicit configuration (``--allow-origin`` or the
  ``CONNECTOR_ALLOWED_ORIGINS`` environment variable). There is no default; the
  launcher refuses to start without one.
* The browser obtains a session only by exchanging a single-use, short-lived
  pairing code that this process prints to the operator's terminal. No HTTP route
  issues pairing codes, and codes are never written to logs, URLs, or files.
* Codes issued here create *browser-scoped* sessions, which can only use targets
  that are already approved; they cannot register, approve, or delete targets.
* Approved targets persist in a target registry file (``--registry``, else
  ``CONNECTOR_TARGET_REGISTRY``, else a per-user application-data default). A
  registry that cannot be loaded stops the launcher; it is never recreated.
* Company TLS uses the CA bundle given by ``--tls-ca`` / ``CONNECTOR_TLS_CA``. An
  invalid CA stops the launcher; without one the connector starts, but every
  company operation fails closed.
* Target lifecycle (register/approve/reapprove/revoke/delete) is managed from this
  terminal: whoever can type here is the operator, the same trust model as the
  pairing codes printed here. There is no HTTP route or token for it, and no
  operator command takes MySQL credentials.
"""

from __future__ import annotations

import argparse
import logging
import os
import shlex
import ssl
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence, TextIO

from connector.policy import CompanyTarget, TARGET_MODE_COMPANY
from connector.registry_store import (
    REGISTRY_PATH_ENV_VAR,
    RegistryError,
    TargetRegistryStore,
    default_registry_path,
)
from connector.server import (
    ALLOWED_ORIGINS_ENV_VAR,
    DEFAULT_BIND_HOST,
    DEFAULT_PORT,
    CompanyTargetOperationError,
    ConnectorRuntime,
    create_app,
    parse_allowed_origins,
)

PAIRING_CODE_TTL_SECONDS = 300
DEFAULT_BROWSER_SESSION_TTL_SECONDS = 3600
MIN_BROWSER_SESSION_TTL_SECONDS = 60
MAX_BROWSER_SESSION_TTL_SECONDS = 8 * 3600
TLS_CA_ENV_VAR = "CONNECTOR_TLS_CA"

logger = logging.getLogger("connector.launcher")


@dataclass(frozen=True)
class LauncherConfig:
    allowed_origins: tuple[str, ...]
    session_ttl_seconds: int = DEFAULT_BROWSER_SESSION_TTL_SECONDS
    # None keeps the registry in memory (library use only); build_config always sets it.
    registry_path: Path | None = None
    tls_ca_path: str | None = None


def _argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m connector",
        description="MySQL DBA Validator local connector (binds to 127.0.0.1:%d)." % DEFAULT_PORT,
    )
    parser.add_argument(
        "--allow-origin",
        action="append",
        default=None,
        metavar="ORIGIN",
        help=(
            "Browser origin allowed to pair with this connector, e.g. "
            "http://127.0.0.1:8420. Repeatable. Overrides %s." % ALLOWED_ORIGINS_ENV_VAR
        ),
    )
    parser.add_argument(
        "--session-ttl",
        type=int,
        default=DEFAULT_BROWSER_SESSION_TTL_SECONDS,
        metavar="SECONDS",
        help="Lifetime of a paired browser session (default %(default)s).",
    )
    parser.add_argument(
        "--registry",
        default=None,
        metavar="PATH",
        help=(
            "Approved-target registry file. Overrides %s; default is a per-user "
            "application-data location." % REGISTRY_PATH_ENV_VAR
        ),
    )
    parser.add_argument(
        "--tls-ca",
        default=None,
        metavar="PATH",
        help=(
            "PEM CA bundle used to verify company MySQL servers. Overrides %s. "
            "Without it, company operations fail closed." % TLS_CA_ENV_VAR
        ),
    )
    return parser


def resolve_registry_path(flag_value: str | None, environ: Mapping[str, str]) -> Path:
    """--registry, then CONNECTOR_TARGET_REGISTRY, then the per-user default."""
    raw = Path(flag_value) if flag_value else default_registry_path(dict(environ))
    return raw.expanduser().absolute()


def validate_tls_ca(path_value: str) -> str:
    """Return the absolute CA path, or raise ValueError if it is not a usable CA bundle."""
    path = Path(path_value).expanduser().absolute()
    if not path.is_file():
        raise ValueError("tls_ca_not_found")
    try:
        context = ssl.create_default_context(cafile=str(path))
    except (ssl.SSLError, OSError, ValueError) as exc:
        raise ValueError("tls_ca_unreadable") from exc
    if context.cert_store_stats().get("x509_ca", 0) < 1:
        raise ValueError("tls_ca_contains_no_ca_certificate")
    return str(path)


def build_config(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
) -> LauncherConfig:
    """Parse and validate launcher configuration. Exits (SystemExit) on any error."""
    parser = _argument_parser()
    args = parser.parse_args(argv)
    env = os.environ if environ is None else environ

    raw_origins: list[str] | str | None
    if args.allow_origin:
        raw_origins = args.allow_origin
    else:
        raw_origins = env.get(ALLOWED_ORIGINS_ENV_VAR)

    try:
        origins = parse_allowed_origins(raw_origins)
    except ValueError as exc:
        parser.error(
            f"invalid allowed origin ({exc}). Use scheme://host[:port]; "
            "http is only allowed for 127.0.0.1, localhost or ::1."
        )
    if not origins:
        parser.error(
            "no allowed browser origin configured. Pass --allow-origin "
            f"(e.g. http://127.0.0.1:8420) or set {ALLOWED_ORIGINS_ENV_VAR}."
        )

    ttl = args.session_ttl
    if not (MIN_BROWSER_SESSION_TTL_SECONDS <= ttl <= MAX_BROWSER_SESSION_TTL_SECONDS):
        parser.error(
            f"--session-ttl must be between {MIN_BROWSER_SESSION_TTL_SECONDS} "
            f"and {MAX_BROWSER_SESSION_TTL_SECONDS} seconds."
        )

    registry_path = resolve_registry_path(args.registry, env)
    if registry_path.is_dir():
        parser.error(f"--registry must be a file path, not a directory: {registry_path}")

    raw_ca = args.tls_ca if args.tls_ca else env.get(TLS_CA_ENV_VAR)
    tls_ca_path = None
    if raw_ca:
        try:
            tls_ca_path = validate_tls_ca(raw_ca)
        except ValueError as exc:
            parser.error(f"invalid TLS CA bundle ({exc}): {raw_ca}")

    return LauncherConfig(
        allowed_origins=tuple(origins),
        session_ttl_seconds=ttl,
        registry_path=registry_path,
        tls_ca_path=tls_ca_path,
    )


def build_runtime(config: LauncherConfig) -> ConnectorRuntime:
    """Create the runtime and load the persisted registry.

    Raises RegistryError if the registry exists but cannot be loaded; the file is
    left untouched (fail closed, never recreated).
    """
    runtime = ConnectorRuntime(
        bind_host=DEFAULT_BIND_HOST,
        port=DEFAULT_PORT,
        allowed_origins=list(config.allowed_origins),
        pairing_ttl_seconds=PAIRING_CODE_TTL_SECONDS,
        session_ttl_seconds=config.session_ttl_seconds,
        company_tls_ca_path=config.tls_ca_path,
        registry_store=(
            TargetRegistryStore(config.registry_path) if config.registry_path is not None else None),
    )
    runtime.load_registry()
    return runtime


def print_pairing_code(runtime: ConnectorRuntime, stream: TextIO) -> str:
    """Issue a browser pairing code and show it to the operator (terminal only)."""
    code = runtime.issue_browser_pairing_token()
    stream.write(
        "\nPairing code (single use, expires in %d seconds):\n\n    %s\n\n"
        "Paste it into the validator page's 'Pair connector' field. "
        "Press Enter here for a new code.\n" % (runtime.pairing_ttl_seconds, code)
    )
    stream.flush()
    return code


OPERATOR_HELP = """Operator commands (this terminal only):
  <Enter>                               print a new browser pairing code
  targets                               list all registered targets
  register <id> <host> <port> <name...> register a target (state: pending)
  approve <id>                          snapshot the host's DNS identity and approve
  reapprove <id>                        explicitly re-approve a needs_reapproval target
  revoke <id>                           stop all use of a target until approved again
  delete <id>                           remove a target; its id can never be reused
  help                                  show this help
MySQL usernames/passwords are never entered here; users supply their own in the page."""


def _format_target_line(target: dict) -> str:
    return "  %-24s %-17s %s:%s  %s" % (
        target["target_id"], target["approval_state"], target["host"], target["port"],
        target["display_name"])


def run_operator_command(runtime: ConnectorRuntime, line: str) -> str:
    """Execute one operator console command and return the text to print.

    Every outcome is a single line naming the action, the target id and a stable
    result or error code. Commands take target metadata only, never credentials.
    """
    try:
        parts = shlex.split(line)
    except ValueError:
        return "error: could not parse command (check quotes); type 'help'"
    if not parts:
        return ""
    command, args = parts[0].lower(), parts[1:]

    if command == "help":
        return OPERATOR_HELP
    if command == "targets":
        targets = runtime.list_company_targets(approved_only=False)
        if not targets:
            return "no targets registered"
        return "\n".join(_format_target_line(target) for target in sorted(
            targets, key=lambda item: item["target_id"]))

    if command == "register":
        if len(args) < 4:
            return "usage: register <id> <host> <port> <name...>"
        target_id, host, port_text, name = args[0], args[1], args[2], " ".join(args[3:])
        try:
            port = int(port_text, 10)
        except ValueError:
            return f"error: register {target_id}: invalid_company_target_port"
        target = CompanyTarget(
            target_id=target_id, display_name=name, mode=TARGET_MODE_COMPANY,
            host=host, port=port, tls_required=True, approval_state="pending")
        action = lambda: runtime.register_company_target(target)
        success = lambda t: f"registered {t.target_id}: {t.host}:{t.port} (pending; run 'approve {t.target_id}')"
    elif command in {"approve", "reapprove", "revoke", "delete"}:
        if len(args) != 1:
            return f"usage: {command} <id>"
        target_id = args[0]
        if command == "approve":
            action = lambda: runtime.approve_company_target(target_id)
        elif command == "reapprove":
            action = lambda: runtime.approve_company_target(target_id, allow_reapproval=True)
        elif command == "revoke":
            action = lambda: runtime.revoke_company_target(target_id)
        else:
            action = lambda: runtime.delete_company_target(target_id)
        past = {"approve": "approved", "reapprove": "re-approved", "revoke": "revoked",
                "delete": "deleted"}[command]
        if command in {"approve", "reapprove"}:
            success = lambda t: "%s %s: %s:%s pinned to %s" % (
                past, t.target_id, t.host, t.port, ", ".join(t.dns_identity_snapshot["addresses"]))
        else:
            success = lambda _t: f"{past} {target_id}"
    else:
        return f"error: unknown command '{command}'; type 'help'"

    try:
        result = action()
    except CompanyTargetOperationError as exc:
        message = f"error: {command} {target_id}: {exc.code}"
    except RegistryError as exc:
        message = f"error: {command} {target_id}: registry_unavailable ({exc}); change not applied"
    except ValueError as exc:
        message = f"error: {command} {target_id}: {exc}"
    else:
        message = success(result)
    logger.info("operator %s", message)
    return message


def _pairing_prompt_loop(runtime: ConnectorRuntime, stdin: TextIO, stdout: TextIO) -> None:
    """Operator console: Enter prints a pairing code; other lines are commands."""
    for line in stdin:
        if not line.strip():
            print_pairing_code(runtime, stdout)
            continue
        stdout.write(run_operator_command(runtime, line) + "\n")
        stdout.flush()


def main(argv: Sequence[str] | None = None) -> int:
    config = build_config(argv)
    try:
        runtime = build_runtime(config)
    except RegistryError as exc:
        sys.stderr.write(
            f"connector: cannot load target registry {config.registry_path} ({exc}).\n"
            "Refusing to start. The file was not modified; fix or move it explicitly.\n")
        return 2
    app = create_app(runtime=runtime)

    targets = runtime.list_company_targets(approved_only=False)
    approved = sum(1 for target in targets if target["approval_state"] == "approved")
    tls_line = config.tls_ca_path or (
        f"NOT CONFIGURED - company operations will fail (use --tls-ca or {TLS_CA_ENV_VAR})")
    sys.stdout.write(
        "MySQL DBA Validator local connector\n"
        f"  listening on : http://{DEFAULT_BIND_HOST}:{DEFAULT_PORT} (loopback only)\n"
        f"  allowed pages: {', '.join(config.allowed_origins)}\n"
        f"  session ttl  : {config.session_ttl_seconds} seconds\n"
        f"  registry     : {config.registry_path} ({len(targets)} target(s), {approved} approved)\n"
        f"  company TLS CA: {tls_line}\n"
        "  Type 'help' here for operator target commands.\n"
    )
    print_pairing_code(runtime, sys.stdout)

    if sys.stdin is not None and sys.stdin.isatty():
        threading.Thread(
            target=_pairing_prompt_loop,
            args=(runtime, sys.stdin, sys.stdout),
            name="connector-pairing-prompt",
            daemon=True,
        ).start()

    import uvicorn  # imported lazily so tests do not need a server loop

    try:
        uvicorn.run(app, host=DEFAULT_BIND_HOST, port=DEFAULT_PORT, log_level="info")
    finally:
        runtime.shutdown()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
