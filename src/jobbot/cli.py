"""jobbot command line.

    jobbot init                    one-time setup + health check
    jobbot login <site>            open a browser so you can log in by hand
    jobbot discover [--site ...]   scrape jobs into the DB
    jobbot score                   score unscored jobs against your resume
    jobbot serve                   review dashboard on localhost:8000
    jobbot apply [--submit]        apply to approved jobs (dry-run by default)
    jobbot status                  what's in the pipeline right now
"""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config import (
    CONFIG_DIR,
    anthropic_api_key,
    ensure_data_dirs,
    load_profile,
    load_search_config,
)
from .db import AppStatus, Job, init_db, session_scope
from .scrapers.registry import SCRAPERS, get_scraper

app = typer.Typer(
    name="jobbot",
    help="Scrape, score, and apply to jobs matched against your resume.",
    no_args_is_help=True,
    add_completion=False,
)
console = Console()


def _ok(msg: str) -> None:
    console.print(f"[green]✓[/green] {msg}")


def _warn(msg: str) -> None:
    console.print(f"[yellow]![/yellow] {msg}")


def _fail(msg: str) -> None:
    console.print(f"[red]✗[/red] {msg}")


def _chromium_installed() -> bool:
    """True if Playwright's Chromium is on disk, without starting the driver."""
    import os
    import sys

    if override := os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
        roots = [Path(override)]
    elif sys.platform == "darwin":
        roots = [Path.home() / "Library" / "Caches" / "ms-playwright"]
    elif sys.platform == "win32":
        roots = [Path.home() / "AppData" / "Local" / "ms-playwright"]
    else:
        roots = [Path.home() / ".cache" / "ms-playwright"]

    return any(
        root.is_dir() and any(child.name.startswith("chromium") for child in root.iterdir())
        for root in roots
    )


@app.command()
def version() -> None:
    """Print the version."""
    console.print(f"jobbot {__version__}")


@app.command()
def init() -> None:
    """Create the database and data directories, then health-check the setup."""
    ensure_data_dirs()
    init_db()
    _ok("Database and data directories ready.")

    problems: list[str] = []

    # Config
    try:
        search = load_search_config()
        sites = search.enabled_sites()
        if sites:
            _ok(f"search.yaml loaded — enabled sites: {', '.join(sites)}")
        else:
            _warn("search.yaml has no enabled sites. Set `enabled: true` on at least one.")
    except Exception as exc:
        problems.append(f"search.yaml is invalid: {exc}")
        _fail(f"search.yaml is invalid: {exc}")

    # Profile
    try:
        profile = load_profile()
        missing = profile.missing_required_fields()
        if missing:
            _warn(
                "profile.yaml is incomplete — you can scrape and score, but not apply.\n"
                + "\n".join(f"    missing: {m}" for m in missing)
            )
        else:
            _ok(f"profile.yaml loaded for {profile.identity.full_name}")
    except FileNotFoundError:
        _warn(
            f"No config/profile.yaml yet.\n"
            f"    cp {CONFIG_DIR / 'profile.example.yaml'} {CONFIG_DIR / 'profile.yaml'}"
        )
    except Exception as exc:
        problems.append(f"profile.yaml is invalid: {exc}")
        _fail(f"profile.yaml is invalid: {exc}")

    # Resume
    try:
        profile = load_profile()
        resume_path = profile.resume_file()
        if resume_path.exists():
            from .resume import get_resume_text

            text = get_resume_text(resume_path)
            _ok(f"Resume parsed — {len(text):,} characters from {resume_path.name}")
        else:
            _warn(f"No resume at {resume_path}. Scoring needs one.")
    except FileNotFoundError:
        pass  # already reported above
    except Exception as exc:
        _fail(f"Could not parse resume: {exc}")

    # API key
    if anthropic_api_key():
        _ok("ANTHROPIC_API_KEY found.")
    else:
        _warn(
            "ANTHROPIC_API_KEY not set. Scoring needs it.\n"
            "    Put it in .env as ANTHROPIC_API_KEY=sk-ant-... , or run `ant auth login`."
        )

    # Playwright browsers. Checked on disk rather than by launching the driver,
    # which would spawn a node process just to answer a yes/no question.
    if _chromium_installed():
        _ok("Playwright Chromium installed.")
    else:
        _warn("Playwright browser missing — run: uv run playwright install chromium")

    console.print()
    if problems:
        _fail(f"{len(problems)} problem(s) need fixing before you can run the pipeline.")
        raise typer.Exit(1)
    _ok("Setup looks good.")


@app.command()
def login(
    site: str = typer.Argument(..., help=f"One of: {', '.join(sorted(SCRAPERS))}"),
    timeout: int = typer.Option(300, help="Seconds to wait for you to finish logging in."),
) -> None:
    """Open a real browser so you can log into a job board by hand.

    Nothing is typed for you and no password is stored — the session cookie
    lives in the browser profile under data/browser/<site>/ and is reused by
    every later run. Scripted logins are the most heavily fingerprinted action
    on these sites, which is exactly why this is manual.
    """
    import time

    from .browser.session import browser_page

    scraper = get_scraper(site)
    init_db()

    console.print(f"Opening [bold]{site}[/bold]. Log in in the browser window.")
    console.print(
        f"[dim]Take as long as you need — up to {timeout // 60} minutes. "
        "Close the window when you're done, or press Ctrl+C here.[/dim]"
    )

    with browser_page(site, headless=False) as page:
        page.goto(scraper.login_url, wait_until="domcontentloaded")
        deadline = time.time() + timeout
        logged_in = False
        try:
            while time.time() < deadline:
                time.sleep(2)
                if page.is_closed():
                    break
                try:
                    # Passive check only. Anything that navigates here would
                    # reload the page out from under whatever the user is
                    # typing — including mid-2FA.
                    if scraper.login_complete(page):
                        logged_in = True
                        break
                except Exception:
                    continue  # mid-navigation; try again on the next tick
        except KeyboardInterrupt:
            pass

        if logged_in:
            # Let the session settle before the window closes, so cookies are
            # flushed to the profile directory.
            try:
                page.wait_for_timeout(2_000)
            except Exception:
                pass

    if logged_in:
        _ok(f"Logged into {site}. Session saved — later runs will be headless.")
    elif time.time() >= deadline:
        _warn(
            f"Timed out after {timeout // 60} minutes without confirming a {site} login.\n"
            "    If you did log in, the session is still saved — try `jobbot discover`.\n"
            f"    For a longer window: jobbot login {site} --timeout 900"
        )
    else:
        _warn(
            f"Could not confirm a {site} login.\n"
            "    If you did log in, the session is still saved — try `jobbot discover` "
            "and see whether it works."
        )


@app.command()
def discover(
    site: list[str] = typer.Option(None, "--site", "-s", help="Limit to these sites."),
    limit: int = typer.Option(None, "--limit", "-n", help="Max new jobs to hydrate."),
    show_browser: bool = typer.Option(False, "--show-browser", help="Run headed, for debugging."),
) -> None:
    """Scrape job boards into the local database."""
    init_db()
    search_cfg = load_search_config()

    targets = list(site) if site else search_cfg.enabled_sites()
    if not targets:
        _warn("No sites enabled. Set `enabled: true` on a site in config/search.yaml.")
        raise typer.Exit(1)

    unknown = [s for s in targets if s not in SCRAPERS]
    if unknown:
        _fail(f"No scraper for: {', '.join(unknown)}. Available: {', '.join(sorted(SCRAPERS))}")
        raise typer.Exit(1)

    from .scrapers.runner import discover_all

    results = discover_all(
        search_cfg,
        sites=targets,
        limit=limit,
        headless=not show_browser,
        on_event=lambda msg: console.print(f"[dim]{msg}[/dim]"),
    )

    console.print()
    total = 0
    for stats in results:
        total += stats.saved
        console.print(stats.summary())
        for err in stats.errors[:3]:
            _warn(f"  {err}")

    console.print()
    if total:
        _ok(f"{total} new job(s) saved. Next: [bold]jobbot score[/bold]")
    else:
        _warn("No new jobs saved.")


@app.command()
def inspect(
    site: str = typer.Argument(..., help="Site to inspect."),
    url: str = typer.Option(None, "--url", help="Page to open (defaults to the search page)."),
) -> None:
    """Dump what a page actually fetches, to calibrate scraper selectors.

    Job boards change their markup constantly. This opens a real logged-in
    session, records every JSON endpoint the page hits and the shape of each
    response, and saves the rendered HTML — so fixing a broken selector is a
    two-minute job instead of a guessing game.
    """
    from .browser.session import browser_page
    from .config import DATA_DIR
    from .scrapers.sniffer import JsonSniffer

    scraper = get_scraper(site)
    target = url or getattr(scraper, "login_url", "").replace("/login/", "/search-jobs/")

    out_dir = DATA_DIR / "inspect"
    out_dir.mkdir(parents=True, exist_ok=True)

    with browser_page(site, headless=False) as page:
        sniffer = JsonSniffer(page, patterns=("/api/", "/graphql", "/voyager"))
        page.goto(target, wait_until="domcontentloaded")
        page.wait_for_timeout(6_000)
        page.mouse.wheel(0, 3_000)
        page.wait_for_timeout(3_000)

        console.print(f"\n[bold]JSON endpoints hit by {target}[/bold]\n")
        console.print(sniffer.dump())

        (out_dir / f"{site}.html").write_text(page.content())
        sniffer.save(out_dir / f"{site}-xhr.json")
        sniffer.detach()

    _ok(f"Saved DOM and XHR capture to {out_dir}/{site}.*")


@app.command()
def score(
    limit: int = typer.Option(None, "--limit", "-n", help="Max jobs to score."),
    batch: bool = typer.Option(False, "--batch", help="Force the Batches API (50% cheaper)."),
    live: bool = typer.Option(False, "--live", help="Force one-request-per-job."),
    rescore: bool = typer.Option(False, "--rescore", help="Re-score jobs that already have a score."),
) -> None:
    """Score unscored jobs against your resume."""
    init_db()

    if batch and live:
        _fail("--batch and --live are mutually exclusive.")
        raise typer.Exit(1)

    if not anthropic_api_key():
        _fail(
            "ANTHROPIC_API_KEY is not set.\n"
            "    Put it in .env as ANTHROPIC_API_KEY=sk-ant-... , or run `ant auth login`."
        )
        raise typer.Exit(1)

    search_cfg = load_search_config()

    try:
        load_profile()
    except FileNotFoundError as exc:
        _fail(str(exc))
        raise typer.Exit(1) from None

    from .scoring.runner import score_pending

    stats = score_pending(
        search_cfg,
        limit=limit,
        force_batch=batch,
        force_live=live,
        rescore=rescore,
        on_event=lambda msg: console.print(msg),
    )

    if not stats.scored and not stats.failed:
        return

    model = search_cfg.model.scoring
    console.print()
    _ok(f"{stats.scored} scored, {stats.failed} failed.")
    console.print(
        f"[dim]Tokens: {stats.input_tokens:,} in / {stats.output_tokens:,} out / "
        f"{stats.cache_read_tokens:,} cache-read  "
        f"(~${stats.estimated_cost_usd(model):.2f})[/dim]"
    )

    # A zero cache-hit ratio means the resume prefix is being invalidated on
    # every call — silently paying full price. Worth shouting about.
    if stats.scored > 1 and stats.cache_read_tokens == 0:
        _warn(
            "No prompt-cache reads across this run. The resume prefix is being "
            "invalidated, so every call paid full price. Check that nothing "
            "variable crept into the system blocks in scoring/prompts.py."
        )
    elif stats.cache_read_tokens:
        console.print(f"[dim]Prompt cache hit ratio: {stats.cache_hit_ratio:.0%}[/dim]")

    for err in stats.errors[:5]:
        _warn(err)

    if stats.scored:
        console.print("\nNext: [bold]jobbot serve[/bold] to review.")


@app.command()
def apply(
    limit: int = typer.Option(None, "--limit", "-n", help="Max applications this run."),
    submit: bool = typer.Option(
        False, "--submit", help="Actually click submit. Without this it's a dry run."
    ),
    show_browser: bool = typer.Option(False, "--show-browser", help="Run headed, for debugging."),
    yes: bool = typer.Option(False, "--yes", "-y", help="Skip the confirmation prompt."),
) -> None:
    """Apply to approved jobs. Dry run unless you pass --submit.

    A dry run fills every field and stops at the submit button, saving a
    screenshot to data/evidence/ so you can check exactly what would have
    been sent.
    """
    init_db()
    search_cfg = load_search_config()

    try:
        profile = load_profile()
    except FileNotFoundError as exc:
        _fail(str(exc))
        raise typer.Exit(1) from None

    if missing := profile.missing_required_fields():
        _fail("config/profile.yaml is incomplete — applying needs these:")
        for item in missing:
            console.print(f"    missing: {item}")
        raise typer.Exit(1)

    if not anthropic_api_key():
        _warn(
            "ANTHROPIC_API_KEY is not set — open-ended questions can't be drafted "
            "and will park for you to answer by hand."
        )

    from .appliers.runner import apply_to_approved, approved_jobs

    pending = approved_jobs(limit)
    if not pending:
        _warn("Nothing approved. Approve some jobs at `jobbot serve` first.")
        return

    if submit and not yes:
        console.print()
        _warn(f"About to submit {len(pending)} real application(s) as {profile.identity.full_name}.")
        for job in pending[:10]:
            console.print(f"    · {job.title} @ {job.company}")
        if len(pending) > 10:
            console.print(f"    … and {len(pending) - 10} more")
        console.print()
        if not typer.confirm("Submit these?"):
            console.print("Nothing submitted.")
            return

    stats = apply_to_approved(
        search_cfg,
        limit=limit,
        submit=submit,
        headless=not show_browser,
        on_event=lambda msg: console.print(msg),
    )

    console.print()
    if submit:
        _ok(f"{stats.submitted} submitted.")
    else:
        _ok(f"{stats.dry_filled} filled (dry run — nothing submitted).")
        console.print("[dim]Check data/evidence/ then re-run with --submit.[/dim]")

    if stats.parked:
        _warn(f"{stats.parked} parked — answer them at `jobbot serve` → Needs input.")
    if stats.manual:
        _warn(f"{stats.manual} need doing by hand — see `jobbot serve` → Manual.")
    if stats.skipped_by_cap:
        console.print(f"[dim]{stats.skipped_by_cap} skipped by daily/per-company caps.[/dim]")
    for err in stats.errors[:5]:
        _fail(err)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="Bind address."),
    port: int = typer.Option(8000, help="Port."),
    reload: bool = typer.Option(False, "--reload", help="Auto-reload on code changes."),
) -> None:
    """Open the review dashboard.

    Binds to localhost only by default — the dashboard shows your resume
    analysis and job pipeline, and has no authentication.
    """
    import uvicorn

    init_db()
    console.print(f"Dashboard: [bold]http://{host}:{port}[/bold]  (Ctrl+C to stop)")
    uvicorn.run(
        "jobbot.web.app:app",
        host=host,
        port=port,
        reload=reload,
        log_level="warning",
    )


@app.command("resume-site")
def resume_site(site: str = typer.Argument(..., help="Site to un-pause.")) -> None:
    """Clear a tripped circuit breaker for a site."""
    from .browser import pacing

    init_db()
    with session_scope() as session:
        pacing.clear_breaker(session, site)
    _ok(f"Circuit breaker cleared for {site}.")


@app.command()
def status() -> None:
    """Show what's currently in the pipeline."""
    init_db()
    with session_scope() as session:
        counts: dict[AppStatus, int] = {}
        for job in session.query(Job).all():
            counts[job.status] = counts.get(job.status, 0) + 1

        if not counts:
            console.print("Nothing in the database yet. Run [bold]jobbot discover[/bold].")
            return

        table = Table(title="Pipeline")
        table.add_column("Status")
        table.add_column("Jobs", justify="right")
        order = [
            AppStatus.NEW,
            AppStatus.SCORED,
            AppStatus.APPROVED,
            AppStatus.APPLYING,
            AppStatus.NEEDS_INPUT,
            AppStatus.SUBMITTED,
            AppStatus.MANUAL,
            AppStatus.FAILED,
            AppStatus.SKIPPED,
        ]
        for st in order:
            if st in counts:
                table.add_row(st.value, str(counts[st]))
        console.print(table)


if __name__ == "__main__":  # pragma: no cover
    app()
