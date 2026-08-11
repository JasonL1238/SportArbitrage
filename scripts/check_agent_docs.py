#!/usr/bin/env python3
"""Keep one rule set for every agent vendor, and keep its pointers true.

`AGENTS.md` is the file anyone edits. Each vendor filename beside it is a
byte-identical generated copy, so Claude Code, Codex, Cursor and whatever comes next
read the same words. This check fails when a copy drifts, when a boundary adapter
orphans itself from the root rules, or when an adapter points at a path that no longer
exists — a stale map costs an agent more than a missing one, because it spends the
tokens before finding out.

The map is two hops now: an adapter routes to a document, and a document routes on to
the one evidence file that holds the subject. So the prose documents are checked too —
`docs/**` and `README.md` — because a check that stops at hop one leaves the mandatory
second hop unguarded, which is where a renamed file actually strands an agent.

    python scripts/check_agent_docs.py          # verify (CI)
    python scripts/check_agent_docs.py --fix    # regenerate the vendor copies
"""

from __future__ import annotations

import ast
import io
import os
import re
import sys
import tokenize
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CANONICAL = "AGENTS.md"

# One line per vendor. A new tool joins by being named here; nothing else changes.
VENDOR_ALIASES = ("CLAUDE.md",)

IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "__pycache__",
        "build",
        "data",
        "dist",
        "node_modules",
        "vendor",
        "venv",
    }
)

_FENCE = re.compile(r"```.*?```", re.DOTALL)
#: A code span opens on a run of backticks and closes on a run of the same length —
#: markdown's own rule, and the reason this is not simply ``` `([^`]+)` ```.  With the
#: naive form a single ``` ``--tier core`` ``` span leaves two backticks unpaired, and
#: every span after it in the document is read *inverted*: the prose becomes the code
#: and the code becomes the gaps.  One such construct on line 8 of the largest evidence
#: file hid every pointer below it.
_INLINE = re.compile(r"(`+)(.+?)\1", re.DOTALL)
#: The target of a markdown link, which is the pointer — the label beside it is only
#: what the sentence calls it.  Checking the label and not the target is worse than
#: checking neither, because ``[`docs/evidence/venues.md`](docs/evidence/<renamed>.md)``
#: then reads as verified.  All three destination spellings, because which one a writer
#: reaches for is a coin flip: inline, inline with a title, and a reference definition.
_LINK = re.compile(r"\]\(\s*<?([^)>\s]+)>?[^)]*\)|^\s*\[[^\]]+\]:\s*<?(\S+?)>?\s*$", re.M)
#: Spans may wrap: this repository's prose is hand-wrapped at ~88 columns, and a
#: pointer that straddles a line break is still a pointer.  Fold the break away *only*
#: where a path can actually continue — after or before a ``/ - _ .`` — because
#: folding every break also welds two ordinary words together, and
#: ``python\n  scripts/check_agent_docs.py`` became a CI failure naming
#: ``pythonscripts/check_agent_docs.py``, a path nobody wrote.  Every other break
#: becomes the space it is, which leaves the span looking like the prose it is.
_WRAP_INSIDE_A_PATH = re.compile(r"(?<=[/\-_.])\n[ \t]*|\n[ \t]*(?=[/\-_.])")
_WRAP = re.compile(r"\n\s*")
#: Prose, a placeholder, or a glob — never a path to open.
_NOT_A_PATH = re.compile(r"[\s<>*?|]")


#: File kinds an adapter names.  A closed list, because "has a dot in it" is not a
#: usable test in a repository whose prose is full of dotted symbols —
#: ``coverage.locality_marking`` and ``settings._lookup`` are not files.  A new kind
#: joins by being named here.
#: ``.env`` is deliberately absent: a rule about one says never to commit it, so the
#: only sentence naming it is a prohibition, and requiring it to exist inverts the rule.
_SUFFIXES = (
    ".md", ".py", ".pyi", ".toml", ".yml", ".yaml", ".txt", ".json", ".mjs", ".js",
    ".cfg", ".ini", ".sh", ".sql", ".html", ".css", ".rst", ".lock",
)


def _is_pathlike(span: str) -> bool:
    """Does this span name a file or directory in this repository?

    A named file needs no separator (`pyproject.toml`, a deleted `HANDOFF.md`), and a
    path with a separator is one whatever it ends in — but a URL is somebody else's
    namespace, and this repository's whole subject is other people's endpoints, so
    those are left alone rather than resolved into nonsense.  So is an absolute path:
    ``/usr/local/`` and ``~/.odds/config.toml`` are facts about the machine, not
    documents in this repository,
    and resolving it was how a sentence in prose became an ``IndexError`` out of the
    CI gate instead of a verdict.
    """
    if _NOT_A_PATH.search(span) or span.startswith(("-", "/", "~")) or "://" in span:
        return False
    return span.endswith("/") or span.endswith(_SUFFIXES) or "/" in span


def discover_adapters(root: Path, filename: str = CANONICAL) -> dict[Path, Path]:
    """Return one adapter per relative directory, pruning generated trees."""
    found: dict[Path, Path] = {}
    for directory, child_directories, filenames in os.walk(root):
        child_directories[:] = sorted(
            name for name in child_directories if name not in IGNORED_DIRECTORIES
        )
        if filename not in filenames:
            continue
        path = Path(directory) / filename
        found[path.relative_to(root).parent] = path
    return found


def pointers(text: str) -> list[str]:
    """Every repository path a document names — in prose, in links, and in commands.

    The commands are the whole reason the fenced blocks are read too: they are what
    an agent is told to run, so a command naming a renamed script is worse than a
    stale sentence.  Placeholders (``tests/test_<area>.py``) fall out on their own
    because a path with a ``<`` in it is not a path.
    """
    out: list[str] = []
    prose = _FENCE.sub("", text)
    spans = [code for _, code in _INLINE.findall(prose)]
    spans += [inline or reference for inline, reference in _LINK.findall(prose)]
    for span in spans:
        span = _WRAP.sub(" ", _WRAP_INSIDE_A_PATH.sub("", span))
        if _is_pathlike(span):
            out.append(span)
    for fence in _FENCE.findall(text):
        for token in re.split(r"[\s`'\"()\[\]]+", fence):
            token = token.rstrip(".,;:")
            # Inside a command a bare word is an argument, and a slashed phrase is
            # usually English ("syntax/import check"), so only a named file or an
            # explicit directory counts. The name is what matters, not the
            # extension: a renamed `deploy.sh` is exactly what this should catch.
            if _is_pathlike(token) and (token.endswith("/") or token.endswith(_SUFFIXES)):
                out.append(token)
    return list(dict.fromkeys(out))


def _walk(base: Path, pointer: str) -> Path | None:
    """Follow *pointer* from *base* one component at a time, matching names exactly.

    Component-wise rather than :meth:`Path.exists`, because the name has to be
    compared case-sensitively: this repository is developed on a case-insensitive
    filesystem and validated on a case-sensitive one, and a pointer that only works
    locally is the worst kind — green here, red in CI, and unopenable for the agent
    that trusted it.  Walking also means ``..`` mid-path, an absolute pointer, and an
    unreadable directory all resolve or decline instead of raising.  Absolute
    pointers never arrive: :func:`_is_pathlike` refuses them as out-of-repository.
    """
    # `docs/testing.md#baseline` names a section of a real file, and
    # `tests/test_x.py::test_name` names a node inside one; both suffixes belong to
    # markdown and to pytest rather than to the filesystem.
    pointer = pointer.split("#", 1)[0].split("::", 1)[0]
    if not pointer:
        return None
    current = base.resolve()
    for part in Path(pointer).parts:
        if part == ".":
            continue
        if part == "..":
            current = current.parent
            continue
        try:
            names = {entry.name for entry in current.iterdir()}
        except (NotADirectoryError, PermissionError, FileNotFoundError, OSError):
            return None
        if part not in names:
            return None
        current = current / part
    return current


def _inside(root: Path, path: Path) -> bool:
    """Is this still the repository?  ``../../../../etc/passwd`` resolves, and is
    not a document this repository can tell anyone to open."""
    return path == root.resolve() or root.resolve() in path.parents


def _is_ignored(pointer: str) -> bool:
    """Does this pointer name a directory that is not in the repository?

    ``data/`` is runtime output and gitignored, so it exists on a developer's machine
    and *not* in a fresh checkout — which is the shape of CI. Resolving it turned a
    correct sentence in the map into a build failure on the first step of a clean
    clone, which is the precise "green here, red in CI" failure this checker exists
    to catch. A pointer at an ignored tree names a fact about where things go, not a
    document to open, so it is accepted without looking.
    """
    parts = [part for part in Path(pointer).parts if part not in (".", "..")]
    return bool(parts) and parts[0] in IGNORED_DIRECTORIES


def _names_a_document_here(pointer: str) -> bool:
    """Is this prose pointer a claim about *this* repository, by its shape alone?

    The bar an adapter is held to is stricter: every backticked span in the map is
    deliberate, so a bare ``HANDOFF.md`` that stops existing is an error there.  A
    prose document is a lab notebook, and this repository's subject is other people's
    servers — ``web/v2/scoreboard``, ``markets/open``, ``api.sx.bet/{markets/active}``
    — which are paths in somebody else's namespace and resolve here only by accident.

    Decided on shape and never on whether the thing exists.  An existence test reads
    backwards: it can only ever fire for a pointer whose first component is still
    there, so renaming a *directory* silently turns the check off, and a bare
    ``state-routing.md`` that stops existing can never be reported at all.  That is the
    opposite of what a stale-pointer check is for.

    So: a bare name is a *document*, since routing on to a document is what this hop
    is; anything deeper has to end in a file kind or a directory slash, which is what
    an endpoint path never does; and a first component carrying a dot *inside* it is
    not a directory of ours.  That last rule covers both shapes it needs to — a
    vendor's host (``api.crypto.com/…``) and a climb out to somebody's relative URL
    (``../assets/app.js``) — while a dot that merely *leads* the name is ours:
    ``.github/workflows/tests.yml`` is this repository's file.
    """
    bare = pointer.split("#", 1)[0]
    parts = [part for part in Path(bare).parts if part != "."]
    if not parts:
        return False
    if len(parts) > 1 and "." in parts[0][1:]:
        return False
    if bare.endswith("/"):
        return True
    if len(parts) == 1:
        return parts[0].endswith(".md")
    return bare.endswith(_SUFFIXES)


def _resolve(root: Path, adapter: Path, pointer: str) -> Path | None:
    """The real path a pointer names, or ``None``.

    Adapters spell paths either relative to themselves (``../../docs/<name>.md``) or from
    the repository root (``tests/test_promos.py``); both are natural to write, so
    both are tried.
    """
    for base in (adapter.parent, root):
        found = _walk(base, pointer)
        if found is not None and _inside(root, found):
            # A trailing slash promises a directory; a file behind one is a wrong
            # pointer that happens to name a real thing.
            if pointer.rstrip().endswith("/") and not found.is_dir():
                continue
            return found
    return None


def check(root: Path, *, fix: bool = False) -> list[str]:
    errors: list[str] = []
    adapters = discover_adapters(root)
    if Path(".") not in adapters:
        return [f"missing {CANONICAL} at the repository root"]

    for alias in VENDOR_ALIASES:
        for directory, stray in discover_adapters(root, alias).items():
            if directory not in adapters:
                errors.append(
                    f"{stray.relative_to(root)} has no {CANONICAL} beside it: "
                    f"delete it or write the rules in {directory / CANONICAL}"
                )

    for directory, adapter in sorted(adapters.items()):
        text = adapter.read_text(encoding="utf-8")

        for alias in VENDOR_ALIASES:
            copy = adapter.with_name(alias)
            label = (directory / alias).as_posix()
            drifted = copy.is_file() and copy.read_text(encoding="utf-8") != text
            if fix:
                # Say what was thrown away.  A copy is the file an agent's tool
                # auto-loads, so it is the one most likely to have been edited by
                # mistake, and regenerating it in silence loses that edit for good.
                if drifted:
                    print(f"- discarded an edit made directly to {label}")
                copy.write_text(text, encoding="utf-8")
                continue
            if not copy.is_file():
                errors.append(f"missing vendor copy: {label}")
            elif drifted:
                errors.append(
                    f"vendor copy drifted from {(directory / CANONICAL).as_posix()}: "
                    f"{label} — put the change in {CANONICAL} and run "
                    "`python scripts/check_agent_docs.py --fix`"
                )

        # A pointer that resolves to *the root file*: the bare string is not enough
        # (unbackticked prose naming it is not a link), and neither is a bare
        # `AGENTS.md`, which resolves to the adapter itself.  An adapter that points
        # at the root and then tells you to ignore it is beyond what a path check
        # can see; that is what review is for.
        canonical = (root / CANONICAL).resolve()
        if directory != Path(".") and not any(
            _resolve(root, adapter, pointer) == canonical for pointer in pointers(text)
        ):
            errors.append(
                f"{(directory / CANONICAL).as_posix()} does not point back at the root "
                f"{CANONICAL}; a boundary adds rules, it never replaces them"
            )

        errors.extend(_dangling(root, adapter, text, (directory / CANONICAL).as_posix()))

    # Hop two.  The adapters route to these, and these route on to the one evidence
    # file that holds a subject, so a rename that strands an agent strands them here.
    for document in discover_prose(root):
        errors.extend(
            _dangling(
                root,
                document,
                document.read_text(encoding="utf-8"),
                document.relative_to(root).as_posix(),
                rooted_only=True,
            )
        )

    # And hop two again, from the other direction: a comment saying *why* the code is
    # the way it is, citing the measurement that settled it.  Those citations were
    # what a rename of an evidence file actually broke — six of them in `src/` alone.
    for module in discover_code(root):
        label = module.relative_to(root).as_posix()
        for pointer in citations(module.read_text(encoding="utf-8")):
            if _resolve(root, module, pointer) is None:
                errors.append(f"{label} points at `{pointer}`, which does not exist")

    return errors


def discover_prose(root: Path) -> list[Path]:
    """The documents an adapter routes to: everything under ``docs/`` and the README."""
    docs = (
        path
        for path in sorted(root.glob("docs/**/*.md"))
        if not set(path.relative_to(root).parts) & IGNORED_DIRECTORIES
    )
    return [*docs, *(path for path in [root / "README.md"] if path.is_file())]


#: A comment naming a document is a pointer with no backticks and no link syntax, so
#: these are found by shape.  ``docs/…`` is unambiguous — nothing else in this
#: repository's code spells a path that way.  Not preceded by a path character, because
#: an adapter citing a vendor's own documentation by URL
#: (``https://github.com/…/docs/offering.md``) is naming somebody else's file.
_IN_CODE = re.compile(r"(?<![A-Za-z0-9_./-])docs/[A-Za-z0-9_./-]+\.md")

#: The one file whose subject *is* dangling pointers, so it names them on purpose.
_ABOUT_POINTERS = ("tests/test_agent_docs.py",)


#: Every tree whose modules explain themselves by citing a measurement.  Named here so
#: the set is one visible line: a checker that silently stopped covering two of three
#: trees still passed every test written about it.
CODE_TREES = ("src", "scripts", "tests")


def discover_code(root: Path) -> list[Path]:
    """The modules whose comments route an agent to a document."""
    excluded = {root / name for name in _ABOUT_POINTERS}
    return [
        path
        for directory in CODE_TREES
        for path in sorted((root / directory).rglob("*.py"))
        if path not in excluded and not set(path.relative_to(root).parts) & IGNORED_DIRECTORIES
    ]


def citations(source: str) -> list[str]:
    """Documents cited in *source*'s comments and docstrings.

    Only those two, because a citation explains why the code is what it is.  A path
    inside an ordinary string literal is the program's data — a CLI default, a fixture
    name — and holding a gate over it makes the gate the reason somebody cannot write
    a default.
    """
    prose: list[str] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc:
                prose.append(doc)
    try:
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                prose.append(token.string)
    except (tokenize.TokenError, IndentationError, SyntaxError):
        pass
    return list(dict.fromkeys(_IN_CODE.findall("\n".join(prose))))


def _dangling(
    root: Path, document: Path, text: str, label: str, *, rooted_only: bool = False
) -> list[str]:
    """Every pointer in *document* that names nothing."""
    errors = []
    for pointer in pointers(text):
        if rooted_only and not _names_a_document_here(pointer):
            continue
        if _is_ignored(pointer) or _resolve(root, document, pointer) is not None:
            continue
        errors.append(f"{label} points at `{pointer}`, which does not exist")
    return errors


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    fix = "--fix" in argv
    errors = check(ROOT, fix=fix)
    if errors:
        print("agent documentation check failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    adapters = len(discover_adapters(ROOT))
    copies = adapters * len(VENDOR_ALIASES)
    verb = "regenerated" if fix else "verified"
    print(
        f"agent documentation check passed ({adapters} adapters, "
        f"{copies} vendor copies {verb}, {len(discover_prose(ROOT))} documents and "
        f"{len(discover_code(ROOT))} modules routed)"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
