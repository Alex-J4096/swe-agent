from pathlib import Path

from src.runtime.session import Session


def format_workdir(workdir: Path) -> str:
    """Use a compact home-relative path when the workspace is under $HOME."""
    resolved_workdir = workdir.resolve()
    resolved_home = Path.home().resolve()

    try:
        relative_path = resolved_workdir.relative_to(resolved_home)
    except ValueError:
        return str(resolved_workdir)

    return str(Path("~") / relative_path) if relative_path.parts else "~"


def render_welcome(session: Session, workdir: Path) -> str:
    """Render the startup panel without terminal control sequences."""
    lines = [
        "    Welcome to TOY SWE CLI!",
        "    Send /help for help information.",
        "",
        f"Directory: {format_workdir(workdir)}",
        f"Session:   {session.session_id}",
        f"Model:     {session.model}",
    ]
    content_width = max(len(line) for line in lines)
    horizontal = "─" * (content_width + 2)

    return "\n".join(
        [
            f"╭{horizontal}╮",
            *[f"│ {line:<{content_width}} │" for line in lines],
            f"╰{horizontal}╯",
        ]
    )


def print_welcome(session: Session, workdir: Path, renderer=None) -> None:
    """Print the startup panel through the shared terminal renderer."""
    if renderer is None:
        from src.ui.console import ConsoleRenderer

        renderer = ConsoleRenderer()

    rendered = render_welcome(session, workdir)
    renderer.show_welcome(rendered)
