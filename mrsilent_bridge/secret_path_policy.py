"""Canonical shared secret-path policy -- single source of truth for
authority_policy.py's GATED_PATH_MARKERS and context_staging.py's
CONTEXT_STAGING_DEFAULT_EXCLUDED_MARKERS, so the two layers can never drift
apart on what counts as a secret-bearing path.

Built from a READ-ONLY metadata audit of pulse5-core-01 (directory/file NAMES,
permissions, .gitignore entries, systemd EnvironmentFile paths, code path
constants) -- no secret file content was read, printed, hashed, or copied to
produce this list.

CONFIRMED_SECRET_BEARING locations found (repo-local, i.e. reachable via a
source_paths boundary check against ROOT):
  - secure_keys/            (mode 0700; real Anthropic/OpenAI/xAI/Google API
                              keys, TH3 MCP bearer token, OAuth login PIN)
  - secrets/                (mode 0700; API key .env files, YouTube/Telegram
                              OAuth tokens and client secrets)
  - secure/                 (mode 0700; e.g. cloudflare.env)
  - services/th3_mcp/oauth_state/  (mode 0700 files; live OAuth
                              clients/codes/tokens -- already .gitignored with
                              the same reasoning as secure_keys/)
  - omniscraper/founder_session_bridge/  (mode 0700; Founder session/auth
                              material)
  - omniscraper/social_cookie_vault/     (mode 0700; browser session cookies
                              -- credential-equivalent)
  - .config/doctl/          (mode 0700; DigitalOcean CLI credentials)
  - client_secret.json, config/ops_console_token.txt,
    config/mr_silent_auth.json  (individual known secret-bearing files whose
                              containing directory name is too generic to
                              blanket-exclude -- config/ legitimately also
                              holds non-secret configuration)
  - any ".env"-prefixed filename anywhere in the repo (already covered by the
    pre-existing ".env" marker in both GATED_PATH_MARKERS and
    CONTEXT_STAGING_DEFAULT_EXCLUDED_MARKERS -- not duplicated here)

Markers below are deliberately either (a) distinctive multi-word compounds
with negligible collision risk against ordinary source (secure_keys,
founder_session_bridge, social_cookie_vault, client_secret,
ops_console_token, mr_silent_auth), or (b) slash-bounded so a short common
word only matches as its own path segment, not as a substring of an unrelated
word (`/secure/` matches a `secure` directory but not `obscure.py` or
`insecure_default`) -- same technique the existing lists already use for
`/backups/`, `/logs/`, `/jobs/`.
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path("/opt/pulse5-core")

# Substring markers, same matching semantics as GATED_PATH_MARKERS /
# CONTEXT_STAGING_DEFAULT_EXCLUDED_MARKERS (both already do
# `marker.lower() in path_str.lower()`), so importers can extend their
# existing tuples with this one directly.
NEW_CONFIRMED_SECRET_MARKERS = (
    "secure_keys",
    "/secure/",           # slash-bounded: matches a `secure` dir, not `obscure`/`insecure`
    "oauth_state",
    "founder_session_bridge",
    "social_cookie_vault",
    "/doctl",
    "client_secret",
    "ops_console_token",
    "mr_silent_auth",
)


def resolve_path(raw) -> Path:
    """Resolve a caller-supplied path to its canonical absolute form,
    collapsing '..'/'.' segments and following symlinks -- so marker
    matching and the ROOT-boundary check both operate on the REAL target,
    not a string an attacker could obscure with traversal or an alias.
    Never raises for a nonexistent path (strict=False is pathlib's default)."""
    return Path(raw).resolve()


def is_within_root(resolved: Path, root: Path = ROOT) -> bool:
    """True iff `resolved` (already-resolved) is ROOT itself or a real
    descendant of it. This is the boundary submit_work(source_paths=...) is
    meant to be bounded to -- anything outside it (e.g. /etc/pulse5/*.env,
    /root/.ssh/id_rsa, a .env.central symlink target outside the repo) must
    never be reachable via source_paths regardless of marker matching."""
    try:
        resolved.relative_to(root)
        return True
    except ValueError:
        return resolved == root


def matches_secret_marker(resolved: Path, markers) -> str | None:
    """Returns the first matching marker, or None. Matches against the
    RESOLVED path string so a raw caller string that hides a marker behind
    '..' segments still gets caught."""
    resolved_str = str(resolved).lower()
    for marker in markers:
        if marker.lower() in resolved_str:
            return marker
    return None
