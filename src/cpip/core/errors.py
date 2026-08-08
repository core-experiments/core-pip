from __future__ import annotations


def valid_reference(value: str) -> bool:
    return bool(value) and all(
        part and all("a" <= character <= "z" for character in part)
        for part in value.split("-")
    )


class CpipError(Exception):
    """Base class for expected command failures."""


class DiagnosticCpipError(CpipError):
    reference: str | None = None

    def __init__(
        self,
        *,
        message: str,
        context: str | None = None,
        note_stmt: str | None = None,
        hint_stmt: str | None = None,
        reference: str | None = None,
    ) -> None:
        super().__init__(message)
        resolved_reference = reference or self.reference
        if resolved_reference is None:
            raise AssertionError("error reference not provided!")
        if not valid_reference(resolved_reference):
            raise AssertionError("error reference must be kebab-case!")
        self.reference = resolved_reference
        self.message = message
        self.context = context
        self.note_stmt = note_stmt
        self.hint_stmt = hint_stmt

    def render(self, *, ascii: bool = False, color: bool = False) -> str:
        return (
            self.render_ascii(color=color)
            if ascii
            else self.render_unicode(color=color)
        )

    def render_header(self, *, color: bool) -> str:
        reference = self.reference
        if reference is None:
            raise AssertionError("error reference not provided!")
        if color:
            return "\x1b[1;31merror\x1b[0m: \x1b[1m" + reference + "\x1b[0m"
        return f"error: {reference}"

    def render_trailers(self, *, color: bool) -> list[str]:
        lines: list[str] = []
        if self.note_stmt is not None:
            prefix = "\x1b[1;35mnote\x1b[0m" if color else "note"
            lines.append(f"{prefix}: {self.note_stmt}")
        if self.hint_stmt is not None:
            prefix = "\x1b[1;36mhint\x1b[0m" if color else "hint"
            lines.append(f"{prefix}: {self.hint_stmt}")
        return lines

    def render_ascii(self, *, color: bool) -> str:
        lines = [self.render_header(color=color), ""]
        lines.extend(self.message.splitlines())
        if self.context is not None:
            lines.append("")
            lines.extend(self.context.splitlines())
        trailers = self.render_trailers(color=color)
        if trailers:
            lines.append("")
            lines.extend(trailers)
        return "\n".join(lines) + "\n"

    def render_unicode(self, *, color: bool) -> str:
        lines = [self.render_header(color=color), ""]
        message_lines = self.message.splitlines()
        if message_lines:
            if self.context is None:
                first_prefix = "\x1b[31m×\x1b[0m" if color else "×"
                rest_prefix = "\x1b[31m \x1b[0m" if color else " "
                lines.append(f"{first_prefix} {message_lines[0]}")
                for line in message_lines[1:]:
                    lines.append(f"{rest_prefix} {line}")
            else:
                first_prefix = "\x1b[31m×\x1b[0m" if color else "×"
                mid_prefix = "\x1b[31m│\x1b[0m" if color else "│"
                ctx_prefix = "\x1b[31m╰─>\x1b[0m" if color else "╰─>"
                ctx_indent = "\x1b[31m   \x1b[0m" if color else "   "
                lines.append(f"{first_prefix} {message_lines[0]}")
                for line in message_lines[1:]:
                    lines.append(f"{mid_prefix} {line}")
                context_lines = self.context.splitlines()
                lines.append(f"{ctx_prefix} {context_lines[0]}")
                for line in context_lines[1:]:
                    lines.append(f"{ctx_indent} {line}")
        trailers = self.render_trailers(color=color)
        if trailers:
            lines.append("")
            lines.extend(trailers)
        return "\n".join(lines) + "\n"

    def __str__(self) -> str:
        return self.render()


class CommandError(CpipError):
    """The command line is invalid or unsupported."""


class ConfigurationError(CommandError):
    """Configuration input or lookup is invalid."""


class InstallationError(CpipError):
    """A package could not be installed."""


class InvalidWheelFilename(InstallationError):
    """The wheel filename is invalid."""


class UnsupportedWheel(InstallationError):
    """The wheel file is structurally unsupported."""


class BuildError(CpipError):
    """A source distribution could not be built."""


class DistributionNotFound(CpipError):
    """No matching distribution could be found."""


class BestVersionAlreadyInstalled(CpipError):
    """The best available version is already installed."""


class ResolutionError(CpipError):
    """Requirements could not be resolved together."""


class HashError(InstallationError):
    """A distribution hash is missing, unsupported, or invalid."""


class HashMissing(HashError):
    """A required hash was not provided."""


class HashMismatch(HashError):
    """A distribution hash does not match the allowed hashes."""


class HashUnpinned(HashError):
    """A requirement is not pinned in require-hashes mode."""


class VcsHashUnsupported(HashError):
    """Version control requirements cannot be hash-verified."""


class DirectoryUrlHashUnsupported(HashError):
    """Directory requirements cannot be hash-verified."""
