#!/usr/bin/env python3
"""Reject private/runtime material before it can enter OWL's public repository.

Tracked files are read from Git's index, not from the working tree. This matters
when a private value has been staged and the working copy is subsequently
redacted. Untracked, non-ignored files are read from the working tree. Findings
contain paths, line numbers, and problem classes, but never suspected values.
"""

from __future__ import annotations

import ast
import ipaddress
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MAX_PUBLIC_FILE_BYTES = 5 * 1024 * 1024

BLOCKED_TOP_LEVEL_DIRECTORIES = {
    "backups",
    "data",
    "database",
    "exports",
    "indexes",
    "imports",
    "logs",
    "media",
    "playwright-report",
    "qa_artifacts",
    "repositories",
    "screenshots",
    "secrets",
    "staticfiles",
    "test-results",
    "test_artifacts",
    "traces",
    "tmp",
    "var",
}
BLOCKED_PATH_PARTS = {
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "htmlcov",
    "venv",
}
BLOCKED_FILE_NAMES = {
    ".coverage",
    ".DS_Store",
    ".netrc",
    ".git-credentials",
    "db.sqlite3",
    "django-secret-key",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
}
BLOCKED_FILE_NAMES_LOWER = {name.lower() for name in BLOCKED_FILE_NAMES}
BLOCKED_SUFFIXES = {
    ".db",
    ".key",
    ".jks",
    ".log",
    ".p12",
    ".p8",
    ".pdf",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
}
BOOKMARK_EXPORT_SUFFIXES = {".csv", ".json", ".jsonl", ".ndjson", ".zip"}
SCREENSHOT_IMAGE_SUFFIXES = {
    ".bmp",
    ".gif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

PRIVATE_KEY_PATTERN = re.compile(r"-----BEGIN (?:DSA |EC |OPENSSH |PGP |RSA )?PRIVATE KEY-----")
AWS_ACCESS_KEY_PATTERN = re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")
GITHUB_TOKEN_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{50,})\b"
)
URL_USERINFO_PATTERN = re.compile(r"\bhttps?://[^\s/@]+@[^\s/<>'\"]+", re.IGNORECASE)
URL_CREDENTIAL_PARAMETER_PATTERN = re.compile(
    r"\bhttps?://[^\s<>'\"]*[?&](?:access[_-]?token|api[_-]?key|password|pat|secret|token)="
    r"(?P<credential_value>[^&#\s<>'\"]+)",
    re.IGNORECASE,
)

IDENTIFIER = r"[A-Za-z][A-Za-z0-9_.-]*"
DIRECT_ASSIGNMENT_PATTERN = re.compile(
    rf"^\s*(?:export\s+)?(?:(?P<quote>[\"'])(?P<quoted_name>{IDENTIFIER})(?P=quote)"
    rf"|(?P<name>{IDENTIFIER}))\s*[:=]\s*(?P<value>.*?)\s*$"
)
JSON_ASSIGNMENT_PATTERN = re.compile(
    rf"(?:^|[{{,])\s*(?P<quote>[\"'])(?P<name>{IDENTIFIER})(?P=quote)\s*:\s*"
    r"(?P<value>\"(?:\\.|[^\"\\])*\"|'(?:\\.|[^'\\])*'|[^,}\n]+)"
)

CREDENTIAL_NAME_SUFFIXES = {
    "ACCESS_KEY",
    "API_KEY",
    "AUTH_TOKEN",
    "CLIENT_SECRET",
    "PASSWORD",
    "PASSWD",
    "PAT",
    "PRIVATE_KEY",
    "SECRET",
    "SECRET_KEY",
    "TOKEN",
}
ENDPOINT_NAME_SUFFIXES = {
    "BASE_URI",
    "BASE_URL",
    "ENDPOINT",
    "ENDPOINT_URL",
    "HOST",
    "SERVER",
    "SERVER_URL",
    "SERVICE_URL",
    "URI",
    "URL",
}
SENSITIVE_HEADER_NAMES = {
    "AUTHORIZATION",
    "COOKIE",
    "HTTP_AUTHORIZATION",
    "HTTP_COOKIE",
    "PROXY_AUTHORIZATION",
    "SET_COOKIE",
}

# These are whole-value rules. A real credential is not excused merely because
# it happens to contain words such as "test", "sample", or "fake".
UNMISTAKABLE_PLACEHOLDERS = {
    "change-me",
    "changeme",
    "dummy",
    "example",
    "not-a-real-secret",
    "not-a-real-token",
    "placeholder",
    "redacted",
    "replace-me",
}
SYNTHETIC_PROJECT_VALUE_PATTERNS = (
    re.compile(
        r"(?:ci-only|local-check-only)-synthetic-secret-key-not-for-real-use-[a-z0-9-]+",
        re.IGNORECASE,
    ),
    re.compile(
        r"synthetic-test-secret-key-only-not-for-real-use-[a-z0-9-]+",
        re.IGNORECASE,
    ),
    re.compile(r"owl-test-pat-never-valid(?:-[a-z0-9-]+)?", re.IGNORECASE),
    re.compile(r"synthetic-[a-z0-9-]*pat-never-valid-[a-z0-9-]+", re.IGNORECASE),
    re.compile(
        r"synthetic-(?:subprocess|test)-secret-key-not-for-real-use-[a-z0-9-]+",
        re.IGNORECASE,
    ),
    re.compile(r"synthetic-value-never-returned", re.IGNORECASE),
)
EXPRESSION_PATTERNS = (
    re.compile(r"\$[A-Za-z_][A-Za-z0-9_]*"),
    re.compile(r"\$\{[A-Za-z_][A-Za-z0-9_]*\}"),
    re.compile(r"(?:os\.)?environ(?:\.get)?\([^\r\n]+\)"),
    re.compile(r"os\.environ\[[^\r\n]+\]"),
    re.compile(r"(?:env|getenv)\([^\r\n]+\)"),
    re.compile(r"settings\.[A-Za-z_][A-Za-z0-9_]*"),
    re.compile(r"[A-Za-z_][A-Za-z0-9_.]*\([^\r\n]*\)(?:\.[A-Za-z_][A-Za-z0-9_]*\([^\r\n]*\))*"),
    re.compile(
        r"(?:[fF][rR]?|[rR][fF])(?:\"[^\"\r\n]*\{[^}\r\n]+\}[^\"\r\n]*\""
        r"|'[^'\r\n]*\{[^}\r\n]+\}[^'\r\n]*')"
    ),
)
DOCUMENTATION_HOSTS = {"example.com", "example.net", "example.org"}
DOCUMENTATION_IPV4_NETWORKS = tuple(
    ipaddress.ip_network(network)
    for network in ("192.0.2.0/24", "198.51.100.0/24", "203.0.113.0/24")
)
DOCUMENTATION_IPV6_NETWORK = ipaddress.ip_network("2001:db8::/32")


@dataclass(frozen=True)
class RepositoryFile:
    """A public-repository candidate and the authoritative place to read it."""

    path: PurePosixPath
    indexed: bool


class ExternalSymlinkError(Exception):
    """Raised when a candidate symlink resolves beyond the repository boundary."""


def _git(*arguments: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        ["git", "-C", str(REPOSITORY_ROOT), *arguments],
        check=True,
        capture_output=True,
    )


def _git_paths(*arguments: str) -> list[PurePosixPath]:
    output = _git("ls-files", "-z", *arguments).stdout.decode("utf-8")
    return [PurePosixPath(item) for item in output.split("\0") if item]


def repository_files() -> list[RepositoryFile]:
    """Return indexed files plus untracked files Git does not ignore."""

    indexed_paths = _git_paths("--cached")
    indexed_set = set(indexed_paths)
    untracked_paths = _git_paths("--others", "--exclude-standard")
    candidates = [RepositoryFile(path, indexed=True) for path in indexed_paths]
    candidates.extend(
        RepositoryFile(path, indexed=False) for path in untracked_paths if path not in indexed_set
    )
    return candidates


def _indexed_mode(path: PurePosixPath) -> str:
    output = _git("ls-files", "--stage", "-z", "--", path.as_posix()).stdout
    record = output.split(b"\0", 1)[0]
    return record.split(b" ", 1)[0].decode("ascii") if record else ""


def _read_candidate(candidate: RepositoryFile) -> tuple[bytes, bool]:
    """Read an indexed blob or an untracked worktree file without mixing sources."""

    if candidate.indexed:
        content = _git("show", f":{candidate.path.as_posix()}").stdout
        return content, _indexed_mode(candidate.path) == "120000"

    worktree_path = REPOSITORY_ROOT.joinpath(*candidate.path.parts)
    if worktree_path.is_symlink():
        target = worktree_path.resolve(strict=False)
        if not target.is_relative_to(REPOSITORY_ROOT.resolve()):
            raise ExternalSymlinkError
    return worktree_path.read_bytes(), worktree_path.is_symlink()


def _indexed_symlink_is_external(path: PurePosixPath, content: bytes) -> bool:
    try:
        target_text = content.decode("utf-8")
    except UnicodeDecodeError:
        return True
    target = REPOSITORY_ROOT.joinpath(*path.parent.parts, target_text).resolve(strict=False)
    return not target.is_relative_to(REPOSITORY_ROOT.resolve())


def _is_bookmark_export(path: PurePosixPath) -> bool:
    if path.suffix.lower() not in BOOKMARK_EXPORT_SUFFIXES:
        return False
    words = set(re.findall(r"[a-z]+", path.as_posix().lower()))
    return bool(words & {"bookmark", "bookmarks"}) and (
        bool(words & {"export", "exports"})
        or path.name.lower() in {"bookmark.json", "bookmarks.json"}
    )


def _is_screenshot_image(path: PurePosixPath) -> bool:
    return path.suffix.lower() in SCREENSHOT_IMAGE_SUFFIXES and bool(
        re.search(r"screen[\s_-]*shots?", path.as_posix(), re.IGNORECASE)
    )


def path_problems(path: PurePosixPath, size: int) -> list[str]:
    """Return public-repository problems evident from a candidate path."""

    problems: list[str] = []
    parts_lower = {part.lower() for part in path.parts}
    name_lower = path.name.lower()

    if path.parts and path.parts[0].lower() in BLOCKED_TOP_LEVEL_DIRECTORIES:
        problems.append("runtime/private top-level directory is present")
    if parts_lower & BLOCKED_PATH_PARTS:
        problems.append("generated environment or cache directory is present")
    if name_lower in BLOCKED_FILE_NAMES_LOWER:
        problems.append("private or generated file name is present")
    if name_lower == ".env" or (name_lower.startswith(".env.") and name_lower != ".env.example"):
        problems.append("environment file is present; only .env.example is allowed")
    if path.suffix.lower() in BLOCKED_SUFFIXES:
        problems.append(
            "runtime database, PDF, log, certificate, or private-key container is present"
        )
    if _is_bookmark_export(path):
        problems.append("bookmark export artifact is present")
    if _is_screenshot_image(path):
        problems.append("screenshot image artifact is present")
    if size > MAX_PUBLIC_FILE_BYTES:
        problems.append("file exceeds the 5 MiB public-fixture limit")

    return problems


def _assignment_name(match: re.Match[str]) -> str:
    name = match.groupdict().get("quoted_name") or match.group("name")
    return _normalized_assignment_name(name)


def _normalized_assignment_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()


def _name_has_suffix(name: str, suffixes: set[str]) -> bool:
    return name in suffixes or any(name.endswith(f"_{suffix}") for suffix in suffixes)


def _literal_value(raw_value: str, *, quoted_key: bool = False) -> str | None:
    """Parse a simple assignment literal; expressions and empty values return None."""

    value = raw_value.strip()
    if value.endswith(","):
        value = value[:-1].rstrip()
    if not value:
        return None

    if value[0:1] in {"'", '"'}:
        try:
            parsed = ast.literal_eval(value)
        except SyntaxError, ValueError:
            return ""
        if not isinstance(parsed, str):
            return None
        parsed = parsed.strip()
        return parsed or None

    value = value.split(" #", 1)[0].strip()
    if not value or value in {"(", "[", "{"}:
        return None
    if value.lower() in {"false", "none", "null", "true"}:
        return None
    if any(pattern.fullmatch(value) for pattern in EXPRESSION_PATTERNS):
        return None
    if quoted_key and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", value):
        return None
    return value


def _is_unmistakable_placeholder(value: str) -> bool:
    lowered = value.lower()
    if lowered in UNMISTAKABLE_PLACEHOLDERS:
        return True
    if re.fullmatch(r"<[^<>\r\n]+>", value):
        return True
    if value and set(value) <= {"*", "x", "X", "-", "_"}:
        return True
    return any(pattern.fullmatch(value) for pattern in SYNTHETIC_PROJECT_VALUE_PATTERNS)


def _is_unmistakable_header_placeholder(value: str) -> bool:
    if _is_unmistakable_placeholder(value):
        return True
    return bool(
        re.fullmatch(
            r"(?:Basic|Bearer)\s+(?:<[^<>\r\n]+>|placeholder|redacted)",
            value,
            re.IGNORECASE,
        )
    )


def _documentation_host(host: str) -> bool:
    host = host.lower().rstrip(".")
    return (
        host in DOCUMENTATION_HOSTS
        or any(host.endswith(f".{domain}") for domain in DOCUMENTATION_HOSTS)
        or host.endswith((".example", ".invalid", ".test"))
    )


def _is_internal_or_private_endpoint(value: str) -> bool:
    stripped_value = value.strip()
    if not stripped_value or (
        "://" not in stripped_value and any(c.isspace() for c in stripped_value)
    ):
        return False
    split_value = stripped_value if "://" in stripped_value else f"//{stripped_value}"
    try:
        host = urlsplit(split_value).hostname
    except ValueError:
        return False
    if not host or _documentation_host(host):
        return False

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        lowered = host.lower().rstrip(".")
        if lowered == "localhost":
            return False
        if "." not in lowered:
            return True
        labels = set(lowered.split("."))
        return bool(labels & {"corp", "internal", "intranet", "lan", "local", "private"})

    if isinstance(address, ipaddress.IPv4Address) and any(
        address in network for network in DOCUMENTATION_IPV4_NETWORKS
    ):
        return False
    if isinstance(address, ipaddress.IPv6Address) and address in DOCUMENTATION_IPV6_NETWORK:
        return False
    if address.is_loopback:
        return False
    return address.is_private or address.is_link_local


def _contains_source_expression(value: str) -> bool:
    return bool("{" in value or "}" in value or re.search(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?", value))


def _assignment_problems(match: re.Match[str]) -> list[str]:
    name = _assignment_name(match)
    literal = _literal_value(
        match.group("value"),
        quoted_key=bool(match.groupdict().get("quote")),
    )
    if literal is None:
        return []

    problems: list[str] = []
    if _name_has_suffix(name, CREDENTIAL_NAME_SUFFIXES) and not _is_unmistakable_placeholder(
        literal
    ):
        problems.append(f"non-placeholder value assigned to {name}")
    if _name_has_suffix(name, ENDPOINT_NAME_SUFFIXES) and _is_internal_or_private_endpoint(literal):
        problems.append(f"literal internal/private endpoint assigned to {name}")
    if name in SENSITIVE_HEADER_NAMES and not _is_unmistakable_header_placeholder(literal):
        problems.append(f"credential-bearing HTTP header assigned to {name}")
    return problems


def _literal_string(node: ast.AST | None) -> str | None:
    """Return only a real Python string literal, never a variable expression."""

    value: str | None = None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        value = node.value
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _literal_string(node.left)
        right = _literal_string(node.right)
        if left is not None and right is not None:
            value = left + right
    elif isinstance(node, ast.JoinedStr):
        parts = [_literal_string(part) for part in node.values]
        if all(part is not None for part in parts):
            value = "".join(part for part in parts if part is not None)
    if value is None or len(value) > MAX_PUBLIC_FILE_BYTES:
        return None
    value = value.strip()
    return value or None


def _named_target(target: ast.AST) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    if isinstance(target, ast.Subscript):
        return _literal_string(target.slice)
    return None


def _literal_assignment_problems(name: str, literal: str) -> list[str]:
    normalized_name = _normalized_assignment_name(name)
    problems: list[str] = []
    if _name_has_suffix(
        normalized_name, CREDENTIAL_NAME_SUFFIXES
    ) and not _is_unmistakable_placeholder(literal):
        problems.append(f"non-placeholder value assigned to {normalized_name}")
    if _name_has_suffix(
        normalized_name, ENDPOINT_NAME_SUFFIXES
    ) and _is_internal_or_private_endpoint(literal):
        problems.append(f"literal internal/private endpoint assigned to {normalized_name}")
    if normalized_name in SENSITIVE_HEADER_NAMES and not _is_unmistakable_header_placeholder(
        literal
    ):
        problems.append(f"credential-bearing HTTP header assigned to {normalized_name}")
    return problems


def _is_enum_class(node: ast.ClassDef) -> bool:
    enum_names = {"Enum", "Flag", "IntEnum", "IntFlag", "StrEnum", "TextChoices"}
    for base in node.bases:
        if isinstance(base, ast.Name) and base.id in enum_names:
            return True
        if isinstance(base, ast.Attribute) and base.attr in enum_names:
            return True
    return False


class _PythonLiteralVisitor(ast.NodeVisitor):
    """Find hard-coded values in sensitive Python assignments without regex guesses."""

    def __init__(self) -> None:
        self.problems: list[tuple[int, str]] = []

    def _record(self, name: str | None, value: ast.AST | None, line_number: int) -> None:
        if not name:
            return
        literal = _literal_string(value)
        if literal is None:
            return
        self.problems.extend(
            (line_number, problem) for problem in _literal_assignment_problems(name, literal)
        )

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if not _is_enum_class(node):
            self.generic_visit(node)
            return
        for statement in node.body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign)):
                value = statement.value
                if value is not None:
                    self.visit(value)
            else:
                self.visit(statement)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._record(_named_target(target), node.value, node.lineno)
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._record(_named_target(node.target), node.value, node.lineno)
            self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._record(_named_target(node.target), node.value, node.lineno)
        self.visit(node.value)

    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            self._record(_literal_string(key), value, getattr(value, "lineno", node.lineno))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for keyword in node.keywords:
            self._record(keyword.arg, keyword.value, keyword.value.lineno)
        self.generic_visit(node)

    def _visit_function_defaults(self, arguments: ast.arguments, *, line_number: int) -> None:
        positional = [*arguments.posonlyargs, *arguments.args]
        if arguments.defaults:
            for argument, default in zip(
                positional[-len(arguments.defaults) :], arguments.defaults, strict=True
            ):
                self._record(argument.arg, default, getattr(default, "lineno", line_number))
        for argument, default in zip(arguments.kwonlyargs, arguments.kw_defaults, strict=True):
            if default is not None:
                self._record(argument.arg, default, getattr(default, "lineno", line_number))

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_defaults(node.args, line_number=node.lineno)
        self.generic_visit(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_defaults(node.args, line_number=node.lineno)
        self.generic_visit(node)


def _python_literal_problems(text: str) -> list[tuple[int, str]] | None:
    try:
        tree = ast.parse(text)
    except SyntaxError, ValueError:
        return None
    visitor = _PythonLiteralVisitor()
    visitor.visit(tree)
    return visitor.problems


def content_problems(text: str, *, path: PurePosixPath | None = None) -> list[tuple[int, str]]:
    """Return suspected private-material locations without returning their values."""

    problems: list[tuple[int, str]] = []
    python_problems = (
        _python_literal_problems(text)
        if path is not None and path.suffix.casefold() == ".py"
        else None
    )
    python_source = python_problems is not None
    for line_number, line in enumerate(text.splitlines(), start=1):
        line_problems: set[str] = set()
        if PRIVATE_KEY_PATTERN.search(line):
            line_problems.add("private-key header found")
        if AWS_ACCESS_KEY_PATTERN.search(line):
            line_problems.add("AWS access-key pattern found")
        if GITHUB_TOKEN_PATTERN.search(line):
            line_problems.add("GitHub token pattern found")
        userinfo_match = URL_USERINFO_PATTERN.search(line)
        if userinfo_match and not _contains_source_expression(userinfo_match.group(0)):
            line_problems.add("credential-bearing URL userinfo found")
        credential_parameter_match = URL_CREDENTIAL_PARAMETER_PATTERN.search(line)
        if credential_parameter_match and not _contains_source_expression(
            credential_parameter_match.group("credential_value")
        ):
            line_problems.add("credential-bearing URL parameter found")

        if not python_source:
            direct_assignment = DIRECT_ASSIGNMENT_PATTERN.match(line)
            if direct_assignment:
                line_problems.update(_assignment_problems(direct_assignment))
            for json_assignment in JSON_ASSIGNMENT_PATTERN.finditer(line):
                line_problems.update(_assignment_problems(json_assignment))

        problems.extend((line_number, problem) for problem in sorted(line_problems))
    if python_problems:
        problems.extend(python_problems)
    problems.sort()
    return problems


def main() -> int:
    """Scan the Git index and untracked, non-ignored working-tree files."""

    findings: list[str] = []
    try:
        candidates = repository_files()
    except (OSError, subprocess.CalledProcessError, UnicodeDecodeError) as error:
        print(f"Unable to inspect Git repository candidates: {error}", file=sys.stderr)
        return 2

    for candidate in candidates:
        try:
            content, is_symlink = _read_candidate(candidate)
        except ExternalSymlinkError:
            findings.append(f"{candidate.path}: symlink resolves outside the repository")
            continue
        except (OSError, subprocess.CalledProcessError) as error:
            findings.append(
                f"{candidate.path}: cannot inspect candidate file ({type(error).__name__})"
            )
            continue

        if (
            candidate.indexed
            and is_symlink
            and _indexed_symlink_is_external(candidate.path, content)
        ):
            findings.append(f"{candidate.path}: symlink resolves outside the repository")
            continue

        for problem in path_problems(candidate.path, len(content)):
            findings.append(f"{candidate.path}: {problem}")

        if len(content) > MAX_PUBLIC_FILE_BYTES:
            continue
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for line_number, problem in content_problems(text, path=candidate.path):
            findings.append(f"{candidate.path}:{line_number}: {problem}")

    if findings:
        print("Public-repository safety check failed:", file=sys.stderr)
        for finding in findings:
            print(f"- {finding}", file=sys.stderr)
        print(
            "Remove the file from Git or replace the value with an unmistakable placeholder. "
            "This check did not modify anything.",
            file=sys.stderr,
        )
        return 1

    print(
        "Public-repository safety check passed for "
        f"{len(candidates)} indexed or untracked, non-ignored candidate files."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
