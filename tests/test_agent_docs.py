"""The one rule set stays one rule set, and its pointers stay true."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

import pytest

from scripts.check_agent_docs import (
    VENDOR_ALIASES,
    check,
    discover_adapters,
    discover_prose,
    pointers,
)


def _repo(tmp_path: Path, *, root_text: str = "root rules\n") -> Path:
    (tmp_path / "AGENTS.md").write_text(root_text, encoding="utf-8")
    for alias in VENDOR_ALIASES:
        (tmp_path / alias).write_text(root_text, encoding="utf-8")
    return tmp_path


def test_adapter_discovery_finds_nested_files_and_prunes_runtime_data(tmp_path) -> None:
    nested = tmp_path / "src" / "feature"
    nested.mkdir(parents=True)
    adapter = nested / "AGENTS.md"
    adapter.write_text("instructions\n", encoding="utf-8")

    ignored = tmp_path / "data" / "capture"
    ignored.mkdir(parents=True)
    (ignored / "AGENTS.md").write_text("runtime artifact\n", encoding="utf-8")

    found = discover_adapters(tmp_path, "AGENTS.md")

    assert found == {adapter.relative_to(tmp_path).parent: adapter}


def test_a_clean_repository_passes(tmp_path) -> None:
    assert check(_repo(tmp_path)) == []


def test_a_drifted_vendor_copy_is_named_with_its_fix(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / VENDOR_ALIASES[0]).write_text("edited only here\n", encoding="utf-8")

    errors = check(root)

    assert len(errors) == 1
    assert VENDOR_ALIASES[0] in errors[0]
    assert "--fix" in errors[0]


def test_a_missing_vendor_copy_is_reported(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / VENDOR_ALIASES[0]).unlink()

    assert [e for e in check(root) if "missing vendor copy" in e]


def test_fix_regenerates_every_vendor_copy(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / VENDOR_ALIASES[0]).write_text("stale\n", encoding="utf-8")

    assert check(root, fix=True) == []
    assert (root / VENDOR_ALIASES[0]).read_text(encoding="utf-8") == "root rules\n"
    assert check(root) == []


def test_a_pointer_at_a_path_that_does_not_exist_fails(tmp_path) -> None:
    root = _repo(tmp_path, root_text="read `docs/gone.md` first\n")
    check(root, fix=True)

    errors = check(root)

    assert len(errors) == 1
    assert "docs/gone.md" in errors[0]


def test_a_pointer_resolves_from_the_adapter_or_from_the_root(tmp_path) -> None:
    root = _repo(tmp_path)
    (root / "tests").mkdir()
    (root / "tests" / "test_thing.py").write_text("", encoding="utf-8")
    nested = root / "src" / "part"
    nested.mkdir(parents=True)
    # One pointer spelled from the root, one spelled relative to the adapter.
    nested.joinpath("AGENTS.md").write_text(
        "See `../../AGENTS.md`. Start with `tests/test_thing.py`.\n", encoding="utf-8"
    )
    check(root, fix=True)

    assert check(root) == []


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("local rules only\n", id="never mentions the root"),
        # The case a substring check waves through.
        pytest.param(
            "Ignore the root AGENTS.md entirely; these rules replace it.\n",
            id="names the root only to disown it",
        ),
        # A bare pointer resolves to the adapter itself, which is not the root.
        pytest.param("These extend `AGENTS.md`.\n", id="points at itself"),
    ],
)
def test_a_boundary_adapter_that_orphans_the_root_rules_fails(tmp_path, body) -> None:
    root = _repo(tmp_path)
    nested = root / "src" / "part"
    nested.mkdir(parents=True)
    nested.joinpath("AGENTS.md").write_text(body, encoding="utf-8")
    check(root, fix=True)

    errors = check(root)

    assert len(errors) == 1
    assert "does not point back at the root" in errors[0]


def test_a_boundary_adapter_that_reaches_the_root_passes(tmp_path) -> None:
    root = _repo(tmp_path)
    nested = root / "src" / "part"
    nested.mkdir(parents=True)
    nested.joinpath("AGENTS.md").write_text("Root rules: `../../AGENTS.md`.\n", encoding="utf-8")
    check(root, fix=True)

    assert check(root) == []


def test_a_pointer_that_only_resolves_case_insensitively_fails(tmp_path) -> None:
    """Green on a Mac and red in CI is the worst way to learn a path is wrong."""
    root = _repo(tmp_path, root_text="read `docs/Architecture.md`\n")
    (root / "docs").mkdir()
    (root / "docs" / "architecture.md").write_text("", encoding="utf-8")
    check(root, fix=True)

    errors = check(root)

    assert len(errors) == 1
    assert "docs/Architecture.md" in errors[0]


def test_fix_says_which_copy_it_overwrote(tmp_path, capsys) -> None:
    """A copy is the file a tool auto-loads, so it is the one edited by mistake."""
    root = _repo(tmp_path)
    (root / VENDOR_ALIASES[0]).write_text("a rule someone added here\n", encoding="utf-8")

    assert check(root, fix=True) == []

    assert VENDOR_ALIASES[0] in capsys.readouterr().out


def test_the_real_repository_keeps_its_entry_point_and_boundaries() -> None:
    """The checker is generic; this is the shape it is checking.

    An empty ``AGENTS.md`` with no boundary adapters is internally consistent and
    would pass every other test here, so the structure itself is pinned: the root
    file carries the rules, and each boundary that has its own vocabulary keeps an
    adapter that points back at them.
    """
    root = Path(__file__).resolve().parents[1]
    text = (root / "AGENTS.md").read_text(encoding="utf-8")

    for heading in ("## Rules", "### Scrape", "## Where things live", "## Commands"):
        assert heading in text, heading
    assert len(text.splitlines()) > 100, "the entry point lost its content"

    for boundary in ("src/sources", "src/promos", "scripts", "tests"):
        assert (root / boundary / "AGENTS.md").is_file(), boundary

    assert check(root) == []


class TestCiRunsWhatTheDocsPromise:
    """`.github/workflows/` and `docs/testing.md` are required to state the same
    commands and change together (AGENTS.md, Edit).

    The case that motivated this: every executable check over
    `src/report_assets.py` runs through `tests/dashboard_smoke.mjs`, and
    `tests/test_report.py` *skips* those cases when Node is absent rather than
    failing. CI installed no Node, so those checks ran only because the runner
    image happened to ship one — a green job that would have stayed green the day
    it stopped.
    """

    @staticmethod
    def _steps() -> list[dict]:
        """The steps CI would actually execute, parsed rather than grepped.

        A substring search over the file text cannot tell a step that runs from
        one that does not, and every way of disabling a step leaves the command
        sitting in the file: a full-line comment, a trailing ``# was: …``, an
        ``if: false``, or the command moved into ``name:`` with ``run: true``.
        All four kept the earlier version of this test green while CI ran
        nothing. Steps carrying any ``if:`` are excluded because this test
        cannot evaluate the expression — a conditional step is not a step this
        check may count on.
        """
        import yaml

        root = Path(__file__).resolve().parents[1]
        text = (root / ".github/workflows/tests.yml").read_text(encoding="utf-8")
        workflow = yaml.safe_load(text)
        steps = [
            step
            # A **job**-level ``if:`` disables every step under it, and reading
            # only the step-level one missed that entirely: adding ``if: false``
            # to the job turned off pytest, ruff, compileall, the doc check and
            # node at once, and this class stayed green.
            for job in workflow["jobs"].values()
            if "if" not in job
            for step in job.get("steps", [])
            if "if" not in step
        ]
        assert steps, "the workflow parsed no unconditional steps"
        return steps

    @classmethod
    def _runs(cls) -> list[str]:
        """What those steps run — ``run:`` values only, never ``name:``.

        Shell comments are stripped, because a block scalar is the last place a
        command can sit and look like it runs::

            - run: |
                # python -m pytest tests/ -q
                echo skipping
        """
        out = []
        for step in cls._steps():
            if "run" not in step:
                continue
            out.append("\n".join(
                line for line in str(step["run"]).splitlines()
                if not line.lstrip().startswith("#")
            ))
        return out

    @classmethod
    def _uses(cls) -> list[str]:
        return [str(step["uses"]) for step in cls._steps() if "uses" in step]

    def test_ci_installs_node_because_the_dashboard_checks_need_it(self) -> None:
        assert any(u.startswith("actions/setup-node") for u in self._uses()), (
            "tests/test_report.py skips its dashboard checks when node is missing, "
            "so CI must install node or those checks silently do not run"
        )

    def test_ci_runs_every_command_the_testing_doc_calls_full_validation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        doc = (root / "docs/testing.md").read_text(encoding="utf-8")
        block = doc.split("## Full validation")[1].split("```")[1]
        commands = [
            line.strip() for line in block.splitlines()[1:]  # drop the ```bash fence's language
            if line.strip()
        ]
        assert len(commands) > 2, "the full-validation block parsed empty"
        runs = self._runs()
        for command in commands:
            assert any(command in run for run in runs), (
                f"docs/testing.md calls {command!r} full validation, and CI does not run it"
            )


class TestTheReadmeSourceCountsAreTrue:
    """The README's headline is the first thing anybody reads, and it was wrong.

    It said 40 registered sources, 16 first-party, 11 sportsbooks, 24 republished,
    when the registry held 41 / 18 / 13 / 23 — and its per-source table had never
    gained rows for `bet365` or `thescore`, two venues the collector had been
    fetching first-party for months. Nothing checked it, because every path in it
    resolved: the numbers were the part that rotted, and a stale count in the
    opening paragraph misinforms every reader before they reach anything true.
    """

    @staticmethod
    def _readme() -> str:
        return (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")

    def test_the_headline_counts_are_the_registry_s(self) -> None:
        from src.sources import registry

        first_party = set(registry.BY_KEY) - set(registry.REPUBLISHED_SOURCE_KEYS)
        kinds = Counter(registry.BY_KEY[key].kind.value for key in first_party)
        # Everything above the Sources table: the opening paragraph AND the
        # sentence that opens `## Sources`, which states the first-party count a
        # second time and was wrong in exactly the same way.
        readme = self._readme()
        marker = "| Source | Kind | Endpoints |"
        assert marker in readme, "the Sources table header moved — this slice is now the whole file"
        head = readme.split(marker)[0]
        for count, what in (
            (len(registry.BY_KEY), "registered sources"),
            (len(first_party), "venues read first-party"),
            (kinds["sportsbook"], "sportsbooks"),
            (kinds["exchange"], "betting exchanges"),
            (kinds["prediction_market"], "prediction markets"),
            (len(registry.REPUBLISHED_SOURCE_KEYS), "republished feeds"),
        ):
            # Exactly this number, and no other, in front of that phrase: a
            # presence test is satisfied by a correct sentence sitting beside
            # the stale one it replaced, which is the shape this whole class
            # exists to catch.
            # `what` is a multi-word phrase and the README wraps its paragraphs
            # by hand, so each of its spaces has to match a line break too — a
            # pure reflow was otherwise reported as a stale count.
            phrase = r"\s+".join(re.escape(word) for word in what.split())
            said = re.findall(rf"(\d[\d,]*)\s+(?:\*\*)?{phrase}", head)
            said = {int(n.replace(",", "")) for n in said}
            assert said == {count}, (
                f"README says {sorted(said) or 'nothing'} {what} above the Sources "
                f"table; the registry holds {count}"
            )
        # The count is also spelled out in words at the top of `## Sources`.
        words = {13: "Thirteen", 16: "Sixteen", 17: "Seventeen", 18: "Eighteen",
                 19: "Nineteen", 20: "Twenty"}
        spelled = words.get(len(first_party))
        assert spelled is not None, (
            f"no spelled form for {len(first_party)} first-party venues — extend this table"
        )
        opener = self._readme().split("## Sources")[1].split("| Source |")[0]
        assert f"{spelled} venues read first-party" in opener, (
            f"`## Sources` opens with {opener.strip()[:60]!r}, not "
            f"{spelled!r} venues read first-party"
        )

    def test_the_sources_table_lists_every_first_party_venue(self) -> None:
        """The table is the reader's map of what is read directly, so a venue
        missing from it is a venue they do not know the pipeline collects."""
        from src.sources import registry

        section = self._readme().split("## Sources")[1].split("\n### Request budget")[0]
        listed = {
            line.strip().strip("|").split("|")[0].strip().strip("`")
            for line in section.splitlines()
            if line.startswith("| `")
        }
        assert len(listed) > 5, "the Sources table parsed empty — has the section moved?"
        expected = set(registry.BY_KEY) - set(registry.REPUBLISHED_SOURCE_KEYS)
        assert listed == expected, (
            f"missing from the table: {sorted(expected - listed)}; "
            f"listed but not registered first-party: {sorted(listed - expected)}"
        )


class TestTheHotspotTableIsTrue:
    """The banner table routes an agent into a 7,000-line file. Nothing checked it.

    Three of its claims were false at once — `src/report_assets.py` was described as
    having no banners when it has 43, `tests/test_adversarial_findings.py` likewise
    with 17, and `src/collector.py`'s row omitted one of four — and the whole tree was
    green, because the checker validates *paths* and no path was wrong. A table that
    sends an agent to grep a marker that is not there costs it exactly the scroll the
    table exists to prevent.
    """

    @staticmethod
    def _rows() -> list[tuple[str, str]]:
        root = Path(__file__).resolve().parents[1]
        text = (root / "AGENTS.md").read_text(encoding="utf-8")
        section = text.split("**Hotspots")[1].split("\n## ")[0]
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in section.splitlines()
            if line.startswith("| `")
        ]
        assert len(rows) > 5, "the hotspot table was not found; this would pass empty"
        return [(row[0].strip("`"), row[-1]) for row in rows]

    def test_every_banner_it_names_is_really_in_that_file(self) -> None:
        root = Path(__file__).resolve().parents[1]
        missing = []
        for name, where in self._rows():
            body = (root / name).read_text(encoding="utf-8")
            banners = {
                line.split("── ", 1)[1].split(" ─", 1)[0].strip()
                for line in body.splitlines()
                if "── " in line and line.lstrip().startswith(("#", "/*"))
            }
            for quoted in re.findall(r"`([^`]+)`", where):
                # A row also carries cross-references and shows the banner *form*;
                # a banner name itself is plain words.
                if "/" in quoted or "…" in quoted:
                    continue
                if quoted in banners or quoted.replace("\\|", "|") in banners:
                    continue
                # A row may name a symbol to search for rather than a banner.
                if quoted in body:
                    continue
                missing.append(f"{name}: `{quoted}`")

        assert missing == []

    def test_a_file_it_calls_bannerless_really_has_none(self) -> None:
        root = Path(__file__).resolve().parents[1]
        wrong = [
            name
            for name, where in self._rows()
            if "no banners" in where.lower()
            and any(
                "── " in line and line.lstrip().startswith(("#", "/*"))
                for line in (root / name).read_text(encoding="utf-8").splitlines()
            )
        ]

        assert wrong == []

    def test_the_line_counts_it_quotes_are_the_real_ones(self) -> None:
        """A wrong size is a wrong decision about whether to open the file at all."""
        root = Path(__file__).resolve().parents[1]
        text = (root / "AGENTS.md").read_text(encoding="utf-8")
        section = text.split("**Hotspots")[1].split("\n## ")[0]
        rows = [
            [cell.strip() for cell in line.strip().strip("|").split("|")]
            for line in section.splitlines()
            if line.startswith("| `")
        ]

        for row in rows:
            name, quoted = row[0].strip("`"), row[1]
            claimed = float(quoted.rstrip("k"))
            real = len((root / name).read_text(encoding="utf-8").splitlines()) / 1000
            assert abs(claimed - real) < 0.15, f"{name}: says {quoted}, is {real:.1f}k"


def test_a_vendor_copy_without_an_adapter_beside_it_fails(tmp_path) -> None:
    root = _repo(tmp_path)
    orphan = root / "src"
    orphan.mkdir()
    (orphan / VENDOR_ALIASES[0]).write_text("rules nobody edits\n", encoding="utf-8")

    errors = check(root)

    assert len(errors) == 1
    assert "has no AGENTS.md beside it" in errors[0]


def test_prose_that_is_not_a_path_is_not_mistaken_for_one() -> None:
    """A symbol, an environment variable and a placeholder are not paths."""
    text = (
        "Call `coverage.locality_marking` and set `ODDS_HTTP_PROXY_<ST>`.\n"
        "Open `docs/architecture.md`, and `pyproject.toml` for the rules.\n"
    )

    assert pointers(text) == ["docs/architecture.md", "pyproject.toml"]


def test_a_pointer_at_an_ignored_tree_passes_without_it_being_there(tmp_path) -> None:
    """`data/` is gitignored runtime output: present locally, absent in a checkout.

    Resolving it passed on a developer's machine and failed on the first CI step of
    a clean clone — the exact green-here-red-there failure this checker exists to
    catch, committed by the checker itself.
    """
    root = _repo(tmp_path, root_text="Runtime output lands in `data/`; never edit it.\n")
    check(root, fix=True)

    assert not (root / "data").exists()
    assert check(root) == []


def test_a_path_outside_this_repository_is_not_a_pointer() -> None:
    """`/usr/local/` and `~/.odds/` are facts about the machine, not documents here.

    Resolving one turned a sentence in prose into an ``IndexError`` out of the CI
    gate — a build failure that names no adapter and no path.
    """
    text = (
        "Install under `/usr/local/`, run `/opt/homebrew/bin/python`, "
        "and keep keys in `~/.odds/config.toml`.\n"
    )

    assert pointers(text) == []


def test_a_pointer_that_climbs_out_of_the_repository_fails(tmp_path) -> None:
    """It resolves to a real file, and is still not this repository's to name.

    The file has to genuinely exist outside the root, or the check would refuse it
    for merely being absent and this would prove nothing.
    """
    outside = tmp_path / "outside.md"
    outside.write_text("somebody else's file\n", encoding="utf-8")
    (tmp_path / "repo").mkdir()
    root = _repo(tmp_path / "repo", root_text="see `../outside.md`\n")
    check(root, fix=True)

    assert outside.exists()
    errors = check(root)

    assert len(errors) == 1
    assert "../outside.md" in errors[0]


def test_an_anchor_names_a_section_of_a_real_file(tmp_path) -> None:
    root = _repo(tmp_path, root_text="see `docs/testing.md#baseline` and `docs/gone.md#x`\n")
    (root / "docs").mkdir()
    (root / "docs" / "testing.md").write_text("", encoding="utf-8")
    check(root, fix=True)

    errors = check(root)

    assert len(errors) == 1
    assert "docs/gone.md#x" in errors[0]


def test_a_trailing_slash_promises_a_directory(tmp_path) -> None:
    """`docs/testing.md/` names a real file through a wrong pointer."""
    root = _repo(tmp_path, root_text="open `docs/testing.md/`\n")
    (root / "docs").mkdir()
    (root / "docs" / "testing.md").write_text("", encoding="utf-8")
    check(root, fix=True)

    errors = check(root)

    assert len(errors) == 1
    assert "docs/testing.md/" in errors[0]


@pytest.mark.parametrize("pointer", ["docs/../README.md", "./docs/", "docs/"])
def test_an_awkwardly_spelled_pointer_still_resolves(tmp_path, pointer) -> None:
    """A mid-path `..` or a `./` prefix names a real file; both must resolve.

    Walking the pointer rather than indexing backwards from the resolved path is
    what makes this true — the index form silently compared the wrong component.
    """
    root = _repo(tmp_path, root_text=f"see `{pointer}`\n")
    (root / "docs").mkdir()
    (root / "README.md").write_text("", encoding="utf-8")
    check(root, fix=True)

    assert check(root) == []


def test_a_url_is_not_a_repository_path() -> None:
    """This repository's subject is other people's endpoints; they are not files."""
    text = (
        "Probe `https://api.example.com/v1/events.json` first.\n"
        "```bash\ncurl -s https://guest.api.example.com/odds/ | head\n```\n"
    )

    assert pointers(text) == []


def test_a_dotted_symbol_is_not_a_file() -> None:
    text = "Ask `coverage.locality_marking`, read `settings._lookup`, set `SourceDescriptor.config`.\n"

    assert pointers(text) == []


def test_a_pointer_wrapped_across_a_line_is_still_checked() -> None:
    """Adapter prose is hand-wrapped, and the check must not lose the long ones."""
    assert pointers("open `docs/\n  architecture.md` next\n") == ["docs/architecture.md"]


def test_a_line_break_between_two_words_does_not_weld_them_into_a_path() -> None:
    """Folding every break away invented `pythonscripts/check_agent_docs.py`.

    The prose here is hand-wrapped at ~88 columns and backticks whole commands, so
    where the break falls is an accident of line length — and it turned a correct
    sentence into a CI failure naming a path nobody had written.
    """
    assert pointers("run `python\n  scripts/check_agent_docs.py` now\n") == []
    # A break at a separator is still the same path, from either side of it.
    assert pointers("see `src/sources/\n  registry.py`\n") == ["src/sources/registry.py"]
    assert pointers("see `src/sources\n  /registry.py`\n") == ["src/sources/registry.py"]


def test_a_link_is_checked_by_its_target_and_not_by_its_label() -> None:
    """The label is what the sentence calls the file; the target is the pointer.

    Checking only the label is worse than checking neither, because the spelling the
    evidence index actually uses puts a real path in the label and the link it would
    strand an agent with in the target.
    """
    assert pointers("See [the map](docs/gone.md).\n") == ["docs/gone.md"]
    assert pointers("See [`docs/testing.md`](docs/gone.md).\n") == [
        "docs/testing.md",
        "docs/gone.md",
    ]
    # A section of this page and somebody else's site are not paths here.
    assert pointers("[here](#a-heading) and [there](https://example.invalid/a/b)\n") == []


def test_a_pytest_node_id_names_the_file_it_lives_in() -> None:
    assert pointers("run `tests/test_report.py::test_a_thing`\n") == [
        "tests/test_report.py::test_a_thing"
    ]


def test_a_double_backtick_span_does_not_invert_the_rest_of_the_document() -> None:
    """Markdown closes a code span on a run of the same length it opened with.

    Read as single backticks, ` ``--tier core`` ` leaves two unpaired, and every span
    after it in the file swaps phase: the code becomes the gaps and the prose becomes
    the spans.  One such construct on line 8 of the largest evidence file hid every
    pointer below it — from a check whose entire job is to find them.
    """
    text = "Pass ``--tier core`` first.\nThen open `docs/architecture.md`.\n"

    assert pointers(text) == ["docs/architecture.md"]
    # And the doubled span is still read as the one thing it is.
    assert pointers("see ``docs/testing.md`` now\n") == ["docs/testing.md"]


def test_the_other_two_link_spellings_are_targets_too() -> None:
    """Which spelling a writer reaches for is a coin flip; all three are pointers."""
    assert pointers('[x](docs/gone.md "The map")\n') == ["docs/gone.md"]
    assert pointers("[x](<docs/gone.md>)\n") == ["docs/gone.md"]
    assert pointers("[m]: docs/gone.md\n") == ["docs/gone.md"]


def test_a_command_naming_a_file_at_the_end_of_a_sentence_still_resolves() -> None:
    """`python scripts/x.py.` — the full stop is the sentence's, not the name's."""
    assert pointers("```bash\nrun scripts/check_agent_docs.py.\n```\n") == [
        "scripts/check_agent_docs.py"
    ]


def test_a_file_that_must_not_exist_is_not_required_to() -> None:
    """A rule about `.env` says never to commit one, so naming it is a prohibition.

    Requiring it to exist inverts the very rule the sentence is stating.
    """
    assert pointers("Credentials never land in a committed `.env`.\n") == []


class TestTheDocumentsAnAdapterRoutesTo:
    """The map is two hops, and only the second one strands anybody.

    An adapter routes to a document and the document routes on to the one evidence
    file holding the subject.  Checking hop one and stopping there passed a rename of
    `docs/evidence/venues.md` that left eighteen references dangling — including six
    in `src/` — so the prose is checked as well.
    """

    @staticmethod
    def _prose(root: Path, body: str, name: str = "docs/thing.md") -> None:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")

    def test_a_document_that_names_a_path_that_does_not_exist_fails(self, tmp_path) -> None:
        root = _repo(tmp_path)
        (root / "src").mkdir()
        self._prose(root, "Measured against `src/gone.py`.\n")
        check(root, fix=True)

        errors = check(root)

        assert len(errors) == 1
        assert "docs/thing.md" in errors[0] and "src/gone.py" in errors[0]

    def test_a_sibling_named_the_natural_way_is_still_this_repositorys_path(
        self, tmp_path
    ) -> None:
        """A document links a neighbour as `evidence/venues.md`, not from the root.

        Recognising only root-relative spellings read every one of those as somebody
        else's namespace and skipped it — which passed a rename of the very file the
        index exists to route to, silently, on both the checker and the suite.
        """
        root = _repo(tmp_path)
        self._prose(root, "# index\n", name="docs/evidence/venues.md")
        self._prose(root, "Open [`evidence/venues.md`](evidence/venues.md).\n", "docs/i.md")
        check(root, fix=True)

        assert check(root) == []

        (root / "docs" / "evidence" / "venues.md").rename(
            root / "docs" / "evidence" / "renamed.md"
        )

        errors = check(root)

        assert len(errors) == 1
        assert "docs/i.md" in errors[0] and "evidence/venues.md" in errors[0]

    def test_renaming_the_directory_is_caught_the_same_as_renaming_the_file(
        self, tmp_path
    ) -> None:
        """Deciding by whether the first component still exists reads backwards.

        Such a rule can only ever fire for a pointer whose directory survived, so a
        renamed directory turns the check off rather than on — silently, and for every
        pointer through it at once, which is the larger accident of the two.
        """
        root = _repo(tmp_path)
        self._prose(root, "# notes\n", name="docs/evidence/venues.md")
        self._prose(root, "Open [`evidence/venues.md`](evidence/venues.md).\n", "docs/i.md")
        check(root, fix=True)
        assert check(root) == []

        (root / "docs" / "evidence").rename(root / "docs" / "notes")

        errors = check(root)

        assert len(errors) == 1
        assert "evidence/venues.md" in errors[0]

    def test_a_bare_document_name_beside_the_one_naming_it_is_checked(
        self, tmp_path
    ) -> None:
        """Evidence files cite each other by bare name — `state-routing.md`.

        Requiring the name to already exist made a missing one unreportable *by
        construction*: the only pointers it could fail on were the ones that were fine.
        """
        root = _repo(tmp_path)
        self._prose(root, "Continued in `state-routing.md`.\n", "docs/evidence/a.md")
        check(root, fix=True)

        errors = check(root)

        assert len(errors) == 1
        assert "state-routing.md" in errors[0]

        self._prose(root, "# routing\n", name="docs/evidence/state-routing.md")
        assert check(root) == []

    def test_a_first_component_with_a_dot_in_it_is_somebody_elses(self, tmp_path) -> None:
        """One rule, both shapes: a vendor's host, and a climb out to their own page.

        A dot that merely *leads* the name is still ours, which is the case that keeps
        `.github/` inside the check.
        """
        root = _repo(tmp_path)
        self._prose(
            root,
            "Asked `api.crypto.com/api/v1/predictions/events` and "
            "`sportsbook.caesars.com/us/il/bet/`; the page loads `../assets/app.js` "
            "from `../v2/events`.\n",
            "docs/e.md",
        )
        check(root, fix=True)

        assert check(root) == []

        (root / ".github").mkdir()
        self._prose(root, "and `.github/workflows/tests.yml`\n", "docs/f.md")
        errors = check(root)

        assert len(errors) == 1
        assert ".github/workflows/tests.yml" in errors[0]

    def test_the_readme_is_one_of_them(self, tmp_path) -> None:
        root = _repo(tmp_path)
        (root / "src").mkdir()
        (root / "README.md").write_text("Start at `src/gone.py`.\n", encoding="utf-8")
        check(root, fix=True)

        assert [e for e in check(root) if "README.md" in e]

    def test_somebody_elses_endpoint_is_not_this_repositorys_path(self, tmp_path) -> None:
        """The evidence files are a lab notebook about other people's servers.

        `web/v2/scoreboard`, `markets/open` and `locations/{st}` are paths in a
        vendor's namespace, and a checker that resolves them here reports a stale
        map every time somebody records a measurement.
        """
        root = _repo(tmp_path)
        self._prose(
            root,
            "Asked `web/v2/scoreboard`, then `markets/open`; "
            "the response named `env.js`.\n",
            name="docs/evidence/notes.md",
        )
        check(root, fix=True)

        assert check(root) == []

    def test_the_evidence_index_routes_to_every_section_that_is_really_there(self) -> None:
        """The index re-lists every evidence section, which is duplicated content.

        Kept, because listing them is what routes a subject to one file out of five
        instead of five reads — and checked here, because duplicated content that
        nothing compares is how an index quietly starts describing a file that has
        moved on.  Compared by count and by date rather than by wording: the index
        abbreviates on purpose (a heading's venue prefix is redundant under a bullet
        that already names the file), and what actually goes stale is a section
        appended, retired, or redated with the index left alone.
        """
        import re

        root = Path(__file__).resolve().parents[1]
        index = (root / "docs" / "SOURCE_FEASIBILITY.md").read_text(encoding="utf-8")
        bullets = re.findall(
            r"- \*\*`(evidence/[a-z\-]+\.md)`\*\* — (.+?)(?=\n- \*\*|\Z)", index, re.DOTALL
        )
        date = re.compile(r"\d{4}-\d{2}-\d{2}")

        listed = dict(bullets)
        found = {
            path.relative_to(root / "docs").as_posix()
            for path in (root / "docs" / "evidence").glob("*.md")
        }
        assert set(listed) == found, "the index and docs/evidence/ disagree on the files"

        for name, body in listed.items():
            headings = [
                line for line in (root / "docs" / name).read_text(encoding="utf-8").splitlines()
                if re.match(r"#{2,3} ", line)
            ]
            entries = [entry for entry in re.sub(r"\s+", " ", body).split("·") if entry.strip()]
            assert len(entries) == len(headings), (
                f"{name}: {len(headings)} sections, {len(entries)} routed to by the index"
            )
            assert set(date.findall(" ".join(headings))) <= set(date.findall(body)), name

    def test_a_comment_that_cites_a_document_is_checked_too(self, tmp_path) -> None:
        """A comment saying *why* the code is this way, citing what settled it.

        Those citations are what a renamed evidence file actually broke — six in
        `src/` alone — and they carry neither backticks nor link syntax, so they are
        found by shape.
        """
        root = _repo(tmp_path)
        (root / "docs").mkdir()
        (root / "docs" / "testing.md").write_text("", encoding="utf-8")
        (root / "src").mkdir()
        (root / "src" / "thing.py").write_text(
            "# measured 2026-08-11, see docs/testing.md\n"
            "# and the rest in docs/evidence/gone.md\n",
            encoding="utf-8",
        )
        check(root, fix=True)

        errors = check(root)

        assert len(errors) == 1
        assert "src/thing.py" in errors[0] and "docs/evidence/gone.md" in errors[0]

    def test_a_citation_is_read_from_prose_and_not_from_the_programs_data(self) -> None:
        """A comment or docstring explains why the code is what it is; a string
        literal is the program's data.

        Reading both made an adapter citing a *vendor's* own documentation by URL into
        a CI failure, and would have made `default="docs/notes.md"` one too — a gate
        that is the reason somebody cannot write a default.
        """
        from scripts.check_agent_docs import citations

        assert citations("# see docs/evidence/venues.md\n") == ["docs/evidence/venues.md"]
        assert citations('"""Why — docs/architecture.md."""\n') == ["docs/architecture.md"]
        assert citations("# https://github.com/kambi/api/blob/main/docs/offering.md\n") == []
        assert citations('add_argument("--notes", default="docs/notes.md")\n') == []

    def test_the_trees_that_are_scanned_are_pinned(self) -> None:
        """Quietly scanning one tree of three passed every test written about it."""
        from scripts.check_agent_docs import CODE_TREES, discover_code

        root = Path(__file__).resolve().parents[1]
        assert CODE_TREES == ("src", "scripts", "tests")

        scanned = discover_code(root)
        for tree in CODE_TREES:
            assert [p for p in scanned if p.is_relative_to(root / tree)], tree

    def test_the_file_whose_subject_is_dangling_pointers_is_left_alone(self) -> None:
        """This very file names paths that do not exist, on purpose.

        Named rather than inferred, so the exemption is one line somebody can see and
        argue with — and so it cannot quietly grow to cover a file that just happens
        to be failing.
        """
        from scripts.check_agent_docs import _ABOUT_POINTERS, discover_code

        root = Path(__file__).resolve().parents[1]
        mine = Path(__file__).resolve()

        assert _ABOUT_POINTERS == ("tests/test_agent_docs.py",)
        assert mine not in discover_code(root)
        assert "docs/gone.md" in mine.read_text(encoding="utf-8")
        # Every other module really is scanned.
        assert root / "src" / "coverage.py" in discover_code(root)

    def test_every_prose_document_is_actually_reached(self) -> None:
        """A glob that quietly matches nothing would make all of this vacuous."""
        root = Path(__file__).resolve().parents[1]
        found = {path.relative_to(root).as_posix() for path in discover_prose(root)}

        assert "README.md" in found
        assert "docs/SOURCE_FEASIBILITY.md" in found
        assert {path for path in found if path.startswith("docs/evidence/")}
        assert found == {
            path.relative_to(root).as_posix()
            for path in [*root.glob("docs/**/*.md"), root / "README.md"]
        }


def test_the_files_a_command_names_are_checked_too() -> None:
    """The commands are what an agent actually runs.

    A command naming a renamed script is worse than a stale sentence, so fenced
    blocks are read as well — while a placeholder stays a placeholder and an
    English phrase that happens to contain a slash stays English.
    """
    text = (
        "```bash\n"
        "python -m pytest tests/test_<area>.py -q    # a placeholder, not a file\n"
        "python -m compileall -q src scripts         # syntax/import check\n"
        "python scripts/check_agent_docs.py --fix\n"
        "bash scripts/deploy.sh && node tests/smoke.js\n"
        "```\n"
    )

    assert pointers(text) == [
        "scripts/check_agent_docs.py",
        "scripts/deploy.sh",
        "tests/smoke.js",
    ]
