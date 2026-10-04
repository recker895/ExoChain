"""Create missing local role secrets without displaying or replacing credentials."""

import secrets
from pathlib import Path
from dotenv import dotenv_values, set_key


def main():
    path = Path(__file__).resolve().parents[1] / ".env"
    values = dotenv_values(path)
    changed = []
    for role in ("OPERATOR_API_TOKEN", "APPROVER_API_TOKEN"):
        if not values.get(role):
            set_key(str(path), role, secrets.token_urlsafe(48))
            changed.append(role)
    print(
        {
            "configured_roles": changed,
            "secret_values_printed": False,
            "storage": "ignored local .env",
        }
    )


if __name__ == "__main__":
    main()
