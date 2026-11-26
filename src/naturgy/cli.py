"""`naturgy` CLI. JSON on stdout, errors as {"error": {...}} on stderr.

Exit codes: 0 success, 1 internal, 2 usage/cancelled, 3 auth, 5 blocked/upstream/claim open,
7 API changed.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Annotated, Any, NoReturn

import typer

from . import api, auth, config, package_version
from .config import default_tokens_path
from .errors import (
    EXIT_AUTH,
    CancelledError,
    InternalError,
    NaturgyError,
    UsageError,
)
from .output import render, render_error
from .tokens import FileTokenStore

app = typer.Typer(
    name="naturgy",
    help="Read your own Naturgy España account (Omega) and file invoice claims. Output is JSON.",
    no_args_is_help=True,
    add_completion=False,
    pretty_exceptions_enable=False,
)
invoices_app = typer.Typer(help="Invoices and invoice PDFs.", no_args_is_help=True)
auth_app = typer.Typer(help="Sign in, check the session and sign out.", no_args_is_help=True)
app.add_typer(invoices_app, name="invoices")
app.add_typer(auth_app, name="auth")


@dataclass
class State:
    tokens_path: Path
    supply: str | None
    raw: bool
    language: str | None


def _store(ctx: typer.Context) -> FileTokenStore:
    state: State = ctx.obj
    return FileTokenStore(state.tokens_path)


def _fail(error: NaturgyError) -> NoReturn:
    typer.echo(render_error(error), err=True)
    raise typer.Exit(error.exit_code)


def _emit(produce: Callable[[], Any]) -> Any:
    try:
        payload = produce()
        text = render(payload)
    except (typer.Exit, typer.Abort):
        raise
    except NaturgyError as error:
        _fail(error)
    except Exception as exc:  # noqa: BLE001 - messages can contain URLs or token values
        _fail(InternalError(f"unexpected internal error: {type(exc).__name__}"))
    typer.echo(text)
    return payload


def _run(
    ctx: typer.Context,
    operation: Callable[[Any, Any], Any],
) -> Any:
    state: State = ctx.obj
    return _emit(
        lambda: api.run_with_client(
            _store(ctx), operation, language=state.language
        )
    )


def _supplies(client: Any, state: State) -> list[dict[str, Any]]:
    supplies = api.get_supplies(client)["supplies"]
    return api.select_supplies(supplies, state.supply)


def _interactive() -> bool:
    return sys.stdin.isatty() and sys.stderr.isatty()


SUPPLY_HELP = (
    "Limit to a supply: service account id, billing ref, contract number, CUPS or a unique prefix/suffix."
)
SupplyOption = Annotated[str | None, typer.Option("--supply", help=SUPPLY_HELP)]


def _apply_supply(ctx: typer.Context, supply: str | None) -> None:
    if supply:
        state: State = ctx.obj
        state.supply = supply


def _print_version(value: bool) -> None:
    if value:
        typer.echo(package_version() or "unknown")
        raise typer.Exit()


@app.callback()
def _root(
    ctx: typer.Context,
    tokens: Annotated[
        Path | None,
        typer.Option(
            "--tokens",
            help="Token file (default: $NATURGY_TOKENS_FILE, a source checkout's "
            ".local-secrets/tokens.json, else ~/.config/naturgy-cli/tokens.json).",
        ),
    ] = None,
    supply: Annotated[
        str | None,
        typer.Option(
            "--supply",
            help="Limit to a supply: service account id, billing ref, contract number, CUPS or a unique suffix.",
        ),
    ] = None,
    raw: Annotated[
        bool,
        typer.Option("--raw", help="Include the original API object as `source` (debugging)."),
    ] = False,
    language: Annotated[
        str | None,
        typer.Option("--language", help="x-language header (default: es)."),
    ] = None,
    version: Annotated[
        bool,
        typer.Option("--version", callback=_print_version, is_eager=True, help="Print the version and exit."),
    ] = False,
) -> None:
    ctx.obj = State(
        tokens_path=tokens.expanduser() if tokens else default_tokens_path(),
        supply=supply,
        raw=raw,
        language=language,
    )


@app.command()
def me(ctx: typer.Context) -> None:
    """Account: holder, contacts, preferred contact and permissions."""
    state: State = ctx.obj
    _run(ctx, lambda client, tokens: api.get_me(client, tokens, raw=state.raw))


@app.command()
def supplies(ctx: typer.Context, supply: SupplyOption = None) -> None:
    """Supplies, contracts, CUPS, addresses, billing accounts and rates."""
    _apply_supply(ctx, supply)
    state: State = ctx.obj

    def operation(client: Any, _tokens: Any) -> Any:
        result = api.get_supplies(client, raw=state.raw)
        selected = api.select_supplies(result["supplies"], state.supply)
        return {"supplies": selected, "count": len(selected)}

    _run(ctx, operation)


@app.command()
def consumption(
    ctx: typer.Context,
    period: Annotated[
        str, typer.Option("--period", help="'month' (default) or 'day'.")
    ] = "month",
    date_from: Annotated[
        str | None,
        typer.Option("--from", help="Start date, YYYY-MM-DD or DD/MM/YYYY."),
    ] = None,
    date_to: Annotated[
        str | None,
        typer.Option("--to", help="End date, YYYY-MM-DD or DD/MM/YYYY."),
    ] = None,
    family: Annotated[
        list[str] | None,
        typer.Option("--family", help="Repeatable: luz, gas, solar, recarga."),
    ] = None,
    supply: SupplyOption = None,
) -> None:
    """Electricity, gas and solar consumption series for the selected supplies."""
    _apply_supply(ctx, supply)
    state: State = ctx.obj

    def operation(client: Any, _tokens: Any) -> Any:
        families = {item.casefold() for item in (family or [])} or None
        if families:
            unknown = families - api.CONSUMPTION_FAMILIES
            if unknown:
                raise UsageError(f"unknown product family: {', '.join(sorted(unknown))}")
        start = api.parse_date(date_from, date.today()) if date_from else None
        end = api.parse_date(date_to, date.today()) if date_to else None
        selected = _supplies(client, state)
        return api.get_consumption(
            client,
            selected,
            period=period,
            start=start,
            end=end,
            families=families,
            raw=state.raw,
        )

    _run(ctx, operation)


@app.command()
def readings(ctx: typer.Context, supply: SupplyOption = None) -> None:
    """Meter readings per contract."""
    _apply_supply(ctx, supply)
    state: State = ctx.obj

    def operation(client: Any, _tokens: Any) -> Any:
        return api.get_readings(client, _supplies(client, state), raw=state.raw)

    _run(ctx, operation)


@app.command()
def debt(ctx: typer.Context, supply: SupplyOption = None) -> None:
    """Pending invoices and other debt per billing account."""
    _apply_supply(ctx, supply)
    state: State = ctx.obj

    def operation(client: Any, _tokens: Any) -> Any:
        return api.get_debt(client, _supplies(client, state), raw=state.raw)

    _run(ctx, operation)


@app.command("cases")
def cases(ctx: typer.Context) -> None:
    """Claims and other cases already filed with Naturgy."""
    state: State = ctx.obj
    _run(ctx, lambda client, _tokens: api.get_cases(client, raw=state.raw))


@invoices_app.command("list")
def invoices_list(ctx: typer.Context, supply: SupplyOption = None) -> None:
    """Invoices across the selected supplies, newest first."""
    _apply_supply(ctx, supply)
    state: State = ctx.obj

    def operation(client: Any, _tokens: Any) -> Any:
        return api.get_invoices(client, _supplies(client, state), raw=state.raw)

    _run(ctx, operation)


@invoices_app.command("show")
def invoices_show(
    ctx: typer.Context,
    invoice_id: Annotated[str, typer.Argument(help="Invoice id from `naturgy invoices list`.")],
) -> None:
    """The detail object for one invoice (API passthrough)."""
    _run(ctx, lambda client, _tokens: api.get_invoice_detail(client, invoice_id))


@invoices_app.command("download")
def invoices_download(
    ctx: typer.Context,
    target: Annotated[
        str, typer.Argument(help="Invoice id, 'latest' or 'all'.")
    ] = "latest",
    directory: Annotated[
        Path, typer.Option("--dir", help="Output directory (created if missing).")
    ] = Path("."),
    supply: SupplyOption = None,
) -> None:
    """Save invoice PDFs as factura_YYYY-MM_<id>.pdf. Existing files are skipped."""
    _apply_supply(ctx, supply)
    state: State = ctx.obj

    def operation(client: Any, tokens: Any) -> Any:
        return api.download_invoices(
            client, tokens, target, directory, _supplies(client, state)
        )

    _run(ctx, operation)


@invoices_app.command("claim")
def invoices_claim(
    ctx: typer.Context,
    target: Annotated[
        str, typer.Argument(help="Invoice id from `naturgy invoices list`, or 'latest'.")
    ],
    message: Annotated[
        str,
        typer.Option(
            "--message",
            "-m",
            help=f"What is wrong with the invoice (max {config.CLAIM_MAX_LENGTH} characters).",
        ),
    ],
    reason: Annotated[
        str | None,
        typer.Option(
            "--reason",
            help="wrong-charge (default), wrong-reading, wrong-invoice, others, "
            "download-invoice, sending-invoice, billed-invoice, or the numeric code.",
        ),
    ] = None,
    attach: Annotated[
        list[Path] | None,
        typer.Option("--attach", help="Attachment: PNG, JPEG or PDF, up to 2 MB. Repeatable."),
    ] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Build the claim and print the JSON body; send nothing.")
    ] = False,
    yes: Annotated[
        bool, typer.Option("--yes", "-y", help="Send without asking for confirmation.")
    ] = False,
    supply: SupplyOption = None,
) -> None:
    """File a bill review ("Revisa tu factura") about one invoice. This SENDS a claim.

    On a terminal it shows what will be sent and asks first; elsewhere it needs --yes.
    --dry-run runs the read-only checks and prints the body without sending.
    """
    _apply_supply(ctx, supply)
    state: State = ctx.obj

    def operation(client: Any, _tokens: Any) -> Any:
        if not (dry_run or yes or _interactive()):
            raise UsageError(
                "not sent: filing a claim needs confirmation; run it in a terminal, or pass --yes"
            )
        draft = api.prepare_claim(
            client,
            _tokens,
            target,
            message,
            reason=reason,
            attachments=attach or [],
            supplies=_supplies(client, state),
        )
        if dry_run:
            return {
                "dry_run": True,
                "invoice_id": draft.invoice_id,
                "reason": draft.reason_code,
                "reason_label": draft.reason_label,
                "payload": api.claim_body(draft),
            }
        if not yes and not _confirm_claim(draft):
            raise CancelledError("not sent: the claim was not confirmed")
        return api.send_claim(client, draft)

    _run(ctx, operation)


def _confirm_claim(draft: api.ClaimDraft) -> bool:
    typer.echo(draft.preview(), err=True)
    typer.echo("¿Enviar esta reclamación a Naturgy? [s/N] ", nl=False, err=True)
    try:
        answer = sys.stdin.readline()
    except KeyboardInterrupt:
        answer = ""
    if not answer.endswith("\n"):
        typer.echo(err=True)
    return answer.strip().casefold() in {"s", "si", "sí"}


@auth_app.command("login")
def auth_login(ctx: typer.Context) -> None:
    """Sign in with NIF/NIE + password and save the session (no browser, no password stored)."""
    nif = typer.prompt("NIF/NIE", err=True)
    password = typer.prompt("Contraseña", hide_input=True, err=True)
    _emit(lambda: auth.login(_store(ctx), nif, password))


@auth_app.command("status")
def auth_status(
    ctx: typer.Context,
    check: Annotated[
        bool,
        typer.Option("--check", help="Also call the API to prove the session still works."),
    ] = False,
) -> None:
    """Token expiry and account; exits 3 if the session expired (or --check is rejected)."""
    store = _store(ctx)
    if check:
        try:
            payload, valid = auth.check(store)
        except NaturgyError as error:
            _fail(error)
        typer.echo(render(payload))
        if not valid:
            raise typer.Exit(EXIT_AUTH)
        return
    payload = _emit(lambda: auth.status(store))
    if payload.get("access_token", {}).get("expired") is True:
        raise typer.Exit(EXIT_AUTH)


@auth_app.command("logout")
def auth_logout(ctx: typer.Context) -> None:
    """Revoke the session (best effort) and delete the local token file."""
    _emit(lambda: auth.logout(_store(ctx)))


@auth_app.command("import")
def auth_import(
    ctx: typer.Context,
    path: Annotated[
        Path,
        typer.Argument(help="JSON exported from the web app's sessionStorage `user` entry."),
    ],
) -> None:
    """Import a browser token export into the token file."""
    _emit(lambda: auth.import_tokens(_store(ctx), path.expanduser()))


def main() -> None:
    app()
