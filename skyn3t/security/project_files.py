"""Shared filename hygiene for project copies (not a secret-content scanner)."""

from pathlib import Path

PRIVATE_PROJECT_NAMES = frozenset({
    ".aws", ".azure", ".docker", ".git", ".git-credentials", ".kube",
    ".netlify", ".netrc", ".npmrc", ".pypirc", ".railway", ".skyn3t",
    ".ssh", ".terraform", ".terraformrc", ".venv", ".vercel", ".vault-token",
    ".wrangler", "__pycache__", "application_default_credentials.json",
    "credentials.json", "id_ed25519", "id_rsa", "node_modules", "pip.conf",
    "pip.ini", "secrets.json", "skyn3t_manifest.json",
})


def is_private_project_path(relative: Path) -> bool:
    """Exclude known credentials, local state, and dependency caches."""
    for part in relative.parts:
        lowered = part.lower()
        if lowered in PRIVATE_PROJECT_NAMES or lowered.startswith((".env", ".dev.vars")):
            return True
        if lowered.endswith((".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")):
            return True
    return False
