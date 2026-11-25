"""launchd/systemd unit rendering from the packaged templates.

Z-8b decisions (owner-confirmed OD-Z7 option a, 2026-11-15, PR #2830):
- Label/unit names and the HYPRIAL_SERVICE_UNIT value follow the per-home
  identity (identity.py); the default home renders byte-identically to the
  pre-Z-8b templates.
- HTTP_PROXY/HTTPS_PROXY/NO_PROXY render at install time (PROXY_KEYS order):
  absent keys render nothing, a present-but-empty value renders as an empty
  value, and values render unfiltered (userinfo exposure in a local proxy URL
  is the owner-accepted known issue KP-081, registered by integration). The
  unit file stays 0644; no credential filtering and no 0600.
- Replacement values are pre-escaped here and ``_replace`` inserts them
  verbatim: launchd scalars via XML escaping, systemd scalars via
  ``_quote_systemd``. The EXECUTABLE_ARGS and PROXY_ENV fragments are
  pre-rendered (scalars inside them already escaped) and bypass the
  per-value escaper.

Z-8 launchd acceptance fix D1 (2026-11-16): the program renders through
``identity.program_path`` (abspath, symlinks kept), so a venv unit runs the
venv's own ``bin/python`` and sees its site-packages.
"""

from __future__ import annotations

import re
from importlib.resources import files
from xml.sax.saxutils import escape as xml_escape

from . import identity
from .types import PROXY_KEYS, ServiceConfig


class ServiceTemplates:
    def render_launchd(self, config: ServiceConfig) -> str:
        template = (
            files("hyprial.daemon.impl")
            .joinpath("services/launchd.plist.template")
            .read_text(encoding="utf-8")
        )
        replacements = {
            "LABEL": self._escape_xml(identity.launchd_label(config)),
            "EXECUTABLE_ARGS": self._launchd_arguments(config),
            "HOME": self._escape_xml(str(config.home.resolve())),
            "PATH": self._escape_xml(config.path_env),
            "HYPRIAL_HOME": self._escape_xml(str(config.hyprial_home.resolve())),
            "STATE_DIR": self._escape_xml(str(config.state_dir.resolve())),
            "STDOUT_LOG": self._escape_xml(
                str((config.state_dir / "daemon-service.stdout.log").resolve())
            ),
            "STDERR_LOG": self._escape_xml(
                str((config.state_dir / "daemon-service.stderr.log").resolve())
            ),
            "PROXY_ENV": self._launchd_proxy_lines(config),
        }
        return self._replace(template, replacements)

    def render_systemd(self, config: ServiceConfig) -> str:
        template = (
            files("hyprial.daemon.impl")
            .joinpath("services/systemd.service.template")
            .read_text(encoding="utf-8")
        )
        replacements = {
            "EXECUTABLE_ARGS": self._systemd_command_line(config),
            "HOME_ENV": self._quote_systemd(f"HOME={config.home.resolve()}"),
            "PATH_ENV": self._quote_systemd(f"PATH={config.path_env}"),
            "HYPRIAL_HOME_ENV": self._quote_systemd(
                f"HYPRIAL_HOME={config.hyprial_home.resolve()}"
            ),
            "STATE_DIR_ENV": self._quote_systemd(
                f"HARNESS_STATE_DIR={config.state_dir.resolve()}"
            ),
            "SERVICE_UNIT": identity.systemd_unit(config),
            "STDOUT_ARG": self._quote_systemd(
                str((config.state_dir / "daemon-service.stdout.log").resolve())
            ),
            "STDERR_ARG": self._quote_systemd(
                str((config.state_dir / "daemon-service.stderr.log").resolve())
            ),
            "PROXY_ENV": self._systemd_proxy_lines(config),
        }
        return self._replace(template, replacements)

    @staticmethod
    def _launchd_arguments(config: ServiceConfig) -> str:
        values = [str(identity.program_path(config)), *config.daemon_args]
        return "\n".join(
            f"    <string>{ServiceTemplates._escape_xml(value)}</string>"
            for value in values
        )

    @staticmethod
    def _launchd_proxy_lines(config: ServiceConfig) -> str:
        return "".join(
            f"    <key>{key}</key>\n"
            f"    <string>{ServiceTemplates._escape_xml(config.proxy_env[key])}</string>\n"
            for key in PROXY_KEYS
            if key in config.proxy_env
        )

    @staticmethod
    def _systemd_command_line(config: ServiceConfig) -> str:
        quoted = ServiceTemplates._quote_systemd(str(identity.program_path(config)))
        return " ".join([quoted, *config.daemon_args])

    @staticmethod
    def _systemd_proxy_lines(config: ServiceConfig) -> str:
        return "".join(
            "Environment="
            + ServiceTemplates._quote_systemd(f"{key}={config.proxy_env[key]}")
            + "\n"
            for key in PROXY_KEYS
            if key in config.proxy_env
        )

    @staticmethod
    def _replace(template: str, replacements: dict[str, str]) -> str:
        def substitute(match: re.Match[str]) -> str:
            key = match.group(1)
            if key not in replacements:
                raise ValueError(f"unknown service template token {key}")
            return replacements[key]

        rendered = re.sub(r"\{\{([A-Z0-9_]+)\}\}", substitute, template)
        remaining = re.findall(r"\{\{[^}]+\}\}", rendered)
        if remaining:
            raise ValueError(f"unresolved service template tokens: {remaining}")
        return rendered

    @staticmethod
    def _escape_xml(value: str) -> str:
        return xml_escape(value, {'"': "&quot;", "'": "&apos;"})

    @staticmethod
    def _quote_systemd(value: str) -> str:
        return (
            '"'
            + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
            + '"'
        )
