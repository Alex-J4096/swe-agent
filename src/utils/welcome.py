from pathlib import Path

from prompt_toolkit import print_formatted_text
from prompt_toolkit.formatted_text import ANSI

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


def print_welcome(session: Session, workdir: Path) -> None:
    """Print the startup panel, with a blue border in capable terminals."""
    rendered = render_welcome(session, workdir)
    lines = rendered.splitlines()
    colored_lines = []

    for index, line in enumerate(lines):
        if index in (0, len(lines) - 1):
            colored_lines.append(f"\033[94m{line}\033[0m")
            continue

        left, content, right = line[0], line[1:-1], line[-1]
        colored_lines.append(
            f"\033[94m{left}\033[0m{content}"
            f"\033[94m{right}\033[0m"
        )

    print_formatted_text(ANSI("\n".join(colored_lines)))
