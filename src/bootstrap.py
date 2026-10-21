"""Exclusive one-shot database bootstrap; exits before any runtime writer starts."""

from app import create_bootstrap_app


def main() -> None:
    create_bootstrap_app()


if __name__ == "__main__":
    main()
