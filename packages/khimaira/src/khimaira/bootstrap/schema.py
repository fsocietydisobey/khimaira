"""Profile manifest schema — the YAML that declares a dev's portable agent setup.

A profile is a YAML file declaring everything `khimaira bootstrap` and
`khimaira sync` need to bring a fresh machine (or update an existing one)
to a known state:

  - dotfiles repo + symlinks (Claude rules, commands, custom config)
  - sibling repos to clone + install (other MCP servers, helper tools)
  - MCP servers to register with Claude Code
  - whether to install the supervisor + build the SPA

Profiles live in the dev's OWN git repo (typically dotfiles). Same
profile applied on N machines yields N matching setups. Khimaira ships
a minimal default for users who don't have a profile yet.

Resolution order when loading:
  1. Explicit --profile arg (path or http(s) URL)
  2. KHIMAIRA_PROFILE env var
  3. ~/.config/khimaira/profile.yaml
  4. Built-in default (khimaira-only, no personal config)
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import Any

import yaml

# Reasonable network timeout for fetching a remote profile. Profiles
# are small (<10KB typically); anything that takes >5s is broken.
_FETCH_TIMEOUT_S = 5.0


@dataclass
class SymlinkEntry:
    """One symlink to create. `src` is relative to the dotfiles repo
    clone; `dest` is an absolute path (may use `~`)."""

    src: str
    dest: str


@dataclass
class DotfilesSpec:
    """The dotfiles repo + the symlinks to create after cloning.

    Optional — a profile with no dotfiles entry just skips the clone
    + symlink phase entirely (useful for "khimaira-only, no personal
    config" setups).
    """

    repo: str
    path: str = "~/dotfiles"  # where to clone it
    branch: str | None = None  # default branch if None
    symlinks: list[SymlinkEntry] = field(default_factory=list)


@dataclass
class RepoSpec:
    """One sibling repo (an MCP server source tree, helper tool, etc.).

    `install` runs in the repo dir after clone. Use it for `uv sync`,
    `pnpm install`, anything one-shot needed before the MCP server
    can run. Skipped if blank.
    """

    name: str
    url: str
    path: str | None = None  # default: ~/dev/<name>
    branch: str | None = None
    install: str = ""

    def resolved_path(self) -> Path:
        return Path(os.path.expanduser(self.path or f"~/dev/{self.name}")).resolve()


@dataclass
class McpServerSpec:
    """One MCP server to register with Claude Code (user scope).

    `command` is the full shell line. Uses `claude mcp add -s user --`
    so it appears in every project automatically. Skipped if a server
    by this name is already registered (idempotent).
    """

    name: str
    command: str


@dataclass
class SupervisorSpec:
    """Whether to install khimaira's host-native supervisor (systemd
    on Linux, launchd on macOS). `auto_install=False` skips entirely —
    user can run `khimaira monitor install-service` themselves later."""

    auto_install: bool = False


@dataclass
class Profile:
    """A complete bootstrap manifest.

    All sections are optional. The minimum useful profile is one with
    `mcp_servers` containing khimaira itself — yields a working khimaira
    install with no personal config.
    """

    name: str = "anonymous"
    description: str = ""
    dotfiles: DotfilesSpec | None = None
    repos: list[RepoSpec] = field(default_factory=list)
    mcp_servers: list[McpServerSpec] = field(default_factory=list)
    supervisor: SupervisorSpec = field(default_factory=SupervisorSpec)
    spa_build: bool = False
    # Whether to (re-)write ~/.claude/settings.json with khimaira's hooks.
    # Lives outside the symlink list because hook command paths embed the
    # local khimaira install path, which differs per machine — symlinking
    # settings.json across machines with different usernames or install
    # locations would write the wrong paths. install-hooks generates the
    # correct paths from THIS machine's khimaira location.
    install_claude_hooks: bool = False

    def to_dict(self) -> dict[str, Any]:
        """For diagnostics / JSON output. Mirrors the YAML schema."""
        out: dict[str, Any] = {"name": self.name}
        if self.description:
            out["description"] = self.description
        if self.dotfiles:
            out["dotfiles"] = {
                "repo": self.dotfiles.repo,
                "path": self.dotfiles.path,
                "branch": self.dotfiles.branch,
                "symlinks": [
                    {"src": s.src, "dest": s.dest} for s in self.dotfiles.symlinks
                ],
            }
        out["repos"] = [
            {k: v for k, v in r.__dict__.items() if v not in (None, "")}
            for r in self.repos
        ]
        out["mcp_servers"] = [
            {"name": m.name, "command": m.command} for m in self.mcp_servers
        ]
        out["supervisor"] = {"auto_install": self.supervisor.auto_install}
        out["spa_build"] = self.spa_build
        out["install_claude_hooks"] = self.install_claude_hooks
        return out


class ProfileError(ValueError):
    """Raised when a profile fails to load or validate. Sub-class of
    ValueError so existing exception handlers catch it without
    knowing about this module."""


def _parse_dict(raw: dict[str, Any]) -> Profile:
    """Validate a parsed YAML dict and construct a Profile.

    Strict on shape (unknown fields → error) so typos surface early.
    Lenient on optional sections (any may be omitted entirely).
    """
    if not isinstance(raw, dict):
        raise ProfileError(f"profile must be a YAML mapping, got {type(raw).__name__}")

    known_top = {
        "name",
        "description",
        "dotfiles",
        "repos",
        "mcp_servers",
        "supervisor",
        "spa_build",
        "install_claude_hooks",
    }
    unknown = set(raw.keys()) - known_top
    if unknown:
        raise ProfileError(
            f"unknown top-level keys in profile: {sorted(unknown)}. "
            f"Known: {sorted(known_top)}."
        )

    dotfiles: DotfilesSpec | None = None
    if "dotfiles" in raw and raw["dotfiles"] is not None:
        df = raw["dotfiles"]
        if not isinstance(df, dict):
            raise ProfileError("dotfiles must be a mapping")
        if "repo" not in df:
            raise ProfileError("dotfiles.repo is required when dotfiles is set")
        symlinks: list[SymlinkEntry] = []
        for entry in df.get("symlinks") or []:
            if not isinstance(entry, dict) or "src" not in entry or "dest" not in entry:
                raise ProfileError(f"each symlink needs src + dest; got {entry!r}")
            symlinks.append(
                SymlinkEntry(src=str(entry["src"]), dest=str(entry["dest"]))
            )
        dotfiles = DotfilesSpec(
            repo=str(df["repo"]),
            path=str(df.get("path") or "~/dotfiles"),
            branch=df.get("branch"),
            symlinks=symlinks,
        )

    repos: list[RepoSpec] = []
    for entry in raw.get("repos") or []:
        if not isinstance(entry, dict) or "name" not in entry or "url" not in entry:
            raise ProfileError(f"each repo needs name + url; got {entry!r}")
        repos.append(
            RepoSpec(
                name=str(entry["name"]),
                url=str(entry["url"]),
                path=entry.get("path"),
                branch=entry.get("branch"),
                install=str(entry.get("install") or ""),
            )
        )

    mcp_servers: list[McpServerSpec] = []
    for entry in raw.get("mcp_servers") or []:
        if not isinstance(entry, dict) or "name" not in entry or "command" not in entry:
            raise ProfileError(f"each mcp_server needs name + command; got {entry!r}")
        mcp_servers.append(
            McpServerSpec(
                name=str(entry["name"]),
                command=str(entry["command"]),
            )
        )

    sup_raw = raw.get("supervisor") or {}
    if not isinstance(sup_raw, dict):
        raise ProfileError("supervisor must be a mapping")
    supervisor = SupervisorSpec(auto_install=bool(sup_raw.get("auto_install", False)))

    return Profile(
        name=str(raw.get("name") or "anonymous"),
        description=str(raw.get("description") or ""),
        dotfiles=dotfiles,
        repos=repos,
        mcp_servers=mcp_servers,
        supervisor=supervisor,
        spa_build=bool(raw.get("spa_build", False)),
        install_claude_hooks=bool(raw.get("install_claude_hooks", False)),
    )


def load_profile(source: str | None = None) -> tuple[Profile, str]:
    """Load a profile from the first matching source.

    Resolution order:
      1. `source` argument (explicit) — path or http(s) URL
      2. KHIMAIRA_PROFILE env var
      3. ~/.config/khimaira/profile.yaml
      4. Built-in default (khimaira-only)

    Returns (profile, source_description). The description goes into
    bootstrap output so the user can confirm which profile got loaded.
    """
    if source:
        return _load_explicit(source), source

    env_src = os.environ.get("KHIMAIRA_PROFILE")
    if env_src:
        return _load_explicit(env_src), f"env KHIMAIRA_PROFILE={env_src}"

    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    user_path = Path(xdg) / "khimaira" / "profile.yaml"
    if user_path.is_file():
        return _load_path(user_path), str(user_path)

    return _load_default(), "built-in default (khimaira-only)"


def _load_explicit(source: str) -> Profile:
    """Path OR http(s) URL → Profile. Errors with a clear message on
    either source type's failure mode."""
    if source.startswith(("http://", "https://")):
        return _load_url(source)
    p = Path(os.path.expanduser(source)).resolve()
    if not p.is_file():
        raise ProfileError(f"profile file not found: {p}")
    return _load_path(p)


def _load_path(path: Path) -> Profile:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as e:
        raise ProfileError(f"failed to read profile {path}: {e}") from e
    return _parse_dict(raw or {})


def _load_url(url: str) -> Profile:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "khimaira-bootstrap"})
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT_S) as resp:
            body = resp.read().decode("utf-8")
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise ProfileError(f"failed to fetch profile {url}: {e}") from e
    try:
        raw = yaml.safe_load(body)
    except yaml.YAMLError as e:
        raise ProfileError(f"profile at {url} is not valid YAML: {e}") from e
    return _parse_dict(raw or {})


def _load_default() -> Profile:
    """Load the khimaira-shipped default profile (khimaira-only install)."""
    # importlib.resources is the right API for package data — works in
    # both source checkouts and installed wheels.
    pkg = resources.files("khimaira.bootstrap")
    text = (pkg / "default_profile.yaml").read_text(encoding="utf-8")
    raw = yaml.safe_load(text)
    return _parse_dict(raw or {})


def dump_profile_json(profile: Profile) -> str:
    """Render a profile as JSON for diagnostic output (`khimaira bootstrap
    --dry-run`). Keeps the to_dict shape stable for log scraping."""
    return json.dumps(profile.to_dict(), indent=2, default=str)
