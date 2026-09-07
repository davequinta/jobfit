"""CLI tests. Offline — `init` only copies bundled templates onto disk.

`jobfit init` is the whole difference between "clone this and read the source to
work out what it wants" and "three commands and it runs", so it gets tests.
"""

import pytest

from jobfit import cli


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_init_writes_the_files_a_new_user_needs(project):
    exit_code = cli.main(["init"])

    assert exit_code == 0
    assert (project / "config.yaml").exists()
    assert (project / "profile" / "stack.yaml").exists()
    assert (project / "profile" / "cv.md").exists()
    assert (project / ".env").exists()


def test_init_writes_usable_content_not_empty_files(project):
    cli.main(["init"])

    config = (project / "config.yaml").read_text()
    assert "weworkremotely" in config
    assert "user_agent" in config
    assert "stack" in (project / "profile" / "stack.yaml").read_text()
    assert "JOBFIT_CONTACT" in (project / ".env").read_text()
    # Stage 3 reads the CV at run time; a new user needs to know its shape.
    assert "Location:" in (project / "profile" / "cv.md").read_text()


def test_init_never_overwrites_an_existing_file(project):
    (project / "config.yaml").write_text("# mine, hand-edited\n")

    cli.main(["init"])

    assert (project / "config.yaml").read_text() == "# mine, hand-edited\n"
    # The files that were missing are still created.
    assert (project / "profile" / "stack.yaml").exists()


def test_init_is_safe_to_run_twice(project):
    cli.main(["init"])
    (project / "profile" / "stack.yaml").write_text("# tuned\n")

    assert cli.main(["init"]) == 0
    assert (project / "profile" / "stack.yaml").read_text() == "# tuned\n"


def test_unknown_command_fails_loudly(project, capsys):
    exit_code = cli.main(["frobnicate"])

    assert exit_code == 2
    assert "frobnicate" in capsys.readouterr().err


def test_no_arguments_prints_usage(project, capsys):
    exit_code = cli.main([])

    assert exit_code == 2
    assert "ingest" in capsys.readouterr().err


def test_stage_commands_are_reachable_from_the_cli():
    # The dispatch table is the contract between `jobfit <stage>` and the stage
    # modules; a renamed stage should break here, not at 3am in cron.
    assert set(cli.COMMANDS) == {"cv", "ingest", "prefilter", "score", "queue",
                                 "label", "eval", "ui"}
    assert all(callable(fn) for fn in cli.COMMANDS.values())


def test_init_warns_that_the_scaffolded_files_hold_personal_data(project, capsys):
    """`init` writes a CV and an API key into the working directory.

    If someone runs it inside a git repo, that is one `git add .` away from a
    public CV. The warning is the only thing standing between them and that.
    """
    cli.main(["init"])

    out = capsys.readouterr().out
    assert "profile/" in out and ".gitignore" in out


def test_the_committed_env_example_matches_the_bundled_template():
    """Two copies of the same file drift. This makes the drift a red test.

    `.env.example` exists so someone reading the repo on GitHub can see what is
    needed without installing anything; `templates/env` is what `jobfit init`
    actually writes. They must say the same thing.
    """
    from pathlib import Path

    repo_root = Path(__file__).parents[1]
    committed = (repo_root / ".env.example").read_text()
    bundled = (repo_root / "src" / "jobfit" / "templates" / "env").read_text()

    assert committed == bundled


@pytest.mark.parametrize("command", ["ingest", "prefilter", "score", "queue", "label", "eval"])
def test_every_stage_builds_its_argument_parser(command, capsys):
    """Caught by a smoke test, not by these: `prefilter` declared `--db` on top
    of the shared parser and died with a duplicate-option error the moment it
    ran. 153 unit tests passed while the command was broken, because none of
    them built the parser."""
    from jobfit import cli

    with pytest.raises(SystemExit) as exit_info:
        cli.COMMANDS[command](["--help"])

    assert exit_info.value.code == 0
    assert "--config" in capsys.readouterr().out
