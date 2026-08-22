"""CV import tests. Offline — a fake client stands in for the conversion call.

`jobfit init` scaffolds a blank `profile/cv.md`, and filling it in by hand is the
single biggest piece of setup friction: everyone already has a CV, it is just in
the wrong format. This converts the one they have.
"""

from pathlib import Path

import pytest

from jobfit import cvimport

RAW = """David Quintanilla
Software Engineer
+503 7868 2451
Full Stack Engineer, TeamsWell, Feb 2023 - Present
Led the frontend team.
"""

CONVERTED = """# David Quintanilla

**Location:** El Salvador (UTC-6). Remote.

## Work experience

### Full Stack Engineer — TeamsWell
"""


class FakeMessages:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return type("R", (), {"content": [type("B", (), {"type": "text", "text": self.text})()]})()


class FakeClient:
    def __init__(self, text=CONVERTED):
        self.messages = FakeMessages(text)


@pytest.fixture
def project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


# --- reading the source document ---------------------------------------------


def test_reads_a_plain_text_cv(project):
    source = project / "cv.txt"
    source.write_text(RAW)

    assert "TeamsWell" in cvimport.extract_text(source)


def test_reads_a_markdown_cv(project):
    source = project / "cv.md"
    source.write_text(RAW)

    assert "TeamsWell" in cvimport.extract_text(source)


def test_an_unsupported_format_says_which_ones_work(project):
    source = project / "cv.docx"
    source.write_bytes(b"PK\x03\x04")

    with pytest.raises(ValueError, match=r"\.pdf"):
        cvimport.extract_text(source)


def test_an_empty_document_is_refused_rather_than_converted(project):
    """A scanned PDF with no text layer extracts to nothing. Converting that
    produces a confident, entirely invented CV."""
    source = project / "cv.txt"
    source.write_text("   \n\n  ")

    with pytest.raises(ValueError, match="no text"):
        cvimport.extract_text(source)


# --- the conversion call -----------------------------------------------------


def test_the_source_cv_goes_in_the_user_message_not_the_instructions():
    request = cvimport.build_request(RAW)

    assert RAW in request["messages"][0]["content"]
    assert RAW not in str(request["system"])


def test_the_instructions_forbid_inventing_facts():
    # The failure mode that matters: a converter that smooths a sparse CV into
    # an impressive one produces a document its owner cannot defend.
    system = str(cvimport.build_request(RAW)["system"]).lower()

    assert "invent" in system or "do not add" in system


def test_convert_returns_the_markdown(project):
    client = FakeClient()

    assert cvimport.convert(client, RAW) == CONVERTED


# --- the command -------------------------------------------------------------


def test_writes_profile_cv_md(project):
    (project / "source.txt").write_text(RAW)

    exit_code = cvimport.main(["source.txt"], client=FakeClient())

    assert exit_code == 0
    assert (project / "profile" / "cv.md").read_text() == CONVERTED


def test_refuses_to_overwrite_an_existing_cv_without_force(project, capsys):
    (project / "source.txt").write_text(RAW)
    (project / "profile").mkdir()
    (project / "profile" / "cv.md").write_text("# hand-tuned\n")

    exit_code = cvimport.main(["source.txt"], client=FakeClient())

    assert exit_code == 2
    assert (project / "profile" / "cv.md").read_text() == "# hand-tuned\n"
    assert "--force" in capsys.readouterr().err


def test_force_overwrites(project):
    (project / "source.txt").write_text(RAW)
    (project / "profile").mkdir()
    (project / "profile" / "cv.md").write_text("# hand-tuned\n")

    assert cvimport.main(["source.txt", "--force"], client=FakeClient()) == 0
    assert (project / "profile" / "cv.md").read_text() == CONVERTED


def test_warns_when_the_result_has_no_location_line(project, capsys):
    """Location eligibility is 20 of the 100 rubric points and almost no CV
    states it. Silently shipping a CV without it costs the user real scores."""
    (project / "source.txt").write_text(RAW)

    cvimport.main(["source.txt"], client=FakeClient("# Name\n\nNo location here.\n"))

    assert "location" in capsys.readouterr().err.lower()


def test_a_missing_source_file_is_an_actionable_message(project, capsys):
    exit_code = cvimport.main(["nope.pdf"], client=FakeClient())

    assert exit_code == 2
    assert "nope.pdf" in capsys.readouterr().err


def test_the_command_is_reachable_from_the_cli():
    from jobfit import cli

    assert "cv" in cli.COMMANDS


def test_todays_date_is_passed_in_because_the_model_does_not_know_it():
    """Found by converting a real CV: the model computed "roughly 6.5 years"
    from a May 2019 start date in August 2026, which is 7 years 3 months. Left
    to itself it reckons against its training cutoff and undercounts — and years
    of experience is exactly what postings state a minimum for."""
    request = cvimport.build_request(RAW, today="2026-08-22")

    assert "2026-08-22" in request["messages"][0]["content"]


def test_the_instructions_tell_it_to_compute_the_years_not_estimate_them():
    system = cvimport.build_request(RAW)["system"].lower()

    assert "never estimate" in system
