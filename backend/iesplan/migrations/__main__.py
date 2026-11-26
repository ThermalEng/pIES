"""CLI entrypoint for running versioned pIES database migrations (`python -m iesplan.migrations`)."""

from __future__ import annotations

from iesplan.migrations.runner import apply_migrations


def main() -> None:
    applied = apply_migrations()
    print(f"Applied migrations: {applied}")


if __name__ == "__main__":
    main()
