"""Form extraction, against a real browser and fixture HTML.

The JS extractor is the piece with the most ways to be quietly wrong — a
missed label, a hidden field treated as real, a radio group emitted twice —
and none of that shows up in unit tests of the Python around it. So this runs
Chromium against a fixture modelled on a real Greenhouse form.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jobbot.appliers.forms import read_fields, upload_resume

FIXTURES = Path(__file__).parent / "fixtures"

playwright_api = pytest.importorskip("playwright.sync_api")


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as pw:
        try:
            browser = pw.chromium.launch(headless=True)
        except Exception as exc:  # browsers not installed
            pytest.skip(f"Chromium unavailable: {exc}")
        yield browser
        browser.close()


@pytest.fixture()
def page(browser):
    page = browser.new_page()
    page.goto((FIXTURES / "greenhouse_like.html").as_uri())
    yield page
    page.close()


@pytest.fixture()
def fields(page):
    return read_fields(page, "#application_form")


def by_label(fields, label):
    return next((f for f in fields if f.question.question == label), None)


class TestLabelResolution:
    def test_explicit_label_for(self, fields):
        assert by_label(fields, "First Name") is not None

    def test_aria_label(self, fields):
        assert by_label(fields, "Why do you want to work here?") is not None

    def test_wrapping_label(self, fields):
        assert by_label(fields, "Expected CTC") is not None

    def test_required_marker_is_stripped_from_the_label(self, fields):
        # "First Name *" must key the answer bank as "First Name", or every
        # form words the same question differently.
        assert by_label(fields, "First Name *") is None
        assert by_label(fields, "First Name") is not None


class TestKinds:
    @pytest.mark.parametrize(
        ("label", "kind"),
        [
            ("First Name", "text"),
            ("Email", "email"),
            ("Phone", "phone"),
            ("Years of experience", "number"),
            ("Do you require visa sponsorship?", "choice"),
            ("Why do you want to work here?", "text"),
            ("I agree to the privacy policy", "bool"),
        ],
    )
    def test_kind_inferred_from_input_type(self, fields, label, kind):
        field = by_label(fields, label)
        assert field is not None, f"{label!r} not extracted"
        assert field.kind == kind

    def test_select_options_are_captured_without_the_placeholder(self, fields):
        field = by_label(fields, "Do you require visa sponsorship?")
        assert field.options == ["Yes", "No"]  # "Select..." dropped

    def test_file_input_is_found_even_without_a_label(self, fields):
        assert any(f.is_file for f in fields)


class TestRequiredness:
    def test_required_attribute_is_read(self, fields):
        assert by_label(fields, "First Name").question.required

    def test_asterisk_in_label_marks_required(self, fields):
        assert by_label(fields, "Do you require visa sponsorship?").question.required

    def test_optional_field_is_not_required(self, fields):
        assert not by_label(fields, "Phone").question.required


class TestRadioGroups:
    def test_group_is_emitted_once_with_its_options(self, fields):
        matches = [f for f in fields if "relocate" in f.question.question.lower()]
        assert len(matches) == 1, "a radio group must not be emitted per-option"
        assert set(matches[0].options) == {"Yes", "No"}


class TestExclusions:
    @pytest.mark.parametrize("name", ["authenticity_token", "disabled_field", "hidden_by_css"])
    def test_non_fillable_controls_are_skipped(self, fields, name):
        assert all(f.name != name for f in fields)

    def test_submit_controls_are_not_treated_as_questions(self, fields):
        labels = [f.question.question.lower() for f in fields]
        assert not any("submit application" == label for label in labels)


class TestFilling:
    def test_text_field_round_trips(self, page, fields):
        from jobbot.appliers.forms import fill_field

        field = by_label(fields, "First Name")
        fill_field(page, field, "Jane")
        assert page.input_value("#first_name") == "Jane"

    def test_select_matches_case_insensitively(self, page, fields):
        from jobbot.appliers.forms import fill_field

        field = by_label(fields, "Do you require visa sponsorship?")
        fill_field(page, field, "no")  # lowercase; option is "No"
        assert page.input_value("#visa") == "No"

    def test_select_matches_on_substring(self, page, fields):
        from jobbot.appliers.forms import fill_field

        field = by_label(fields, "Do you require visa sponsorship?")
        fill_field(page, field, "No, I am authorized to work")
        assert page.input_value("#visa") == "No"

    def test_unmatchable_option_raises_rather_than_picking_wrong(self, page, fields):
        from jobbot.appliers.forms import FillError, fill_field

        field = by_label(fields, "Do you require visa sponsorship?")
        with pytest.raises(FillError):
            fill_field(page, field, "Maybe someday")

    def test_checkbox_toggles(self, page, fields):
        from jobbot.appliers.forms import fill_field

        field = by_label(fields, "I agree to the privacy policy")
        fill_field(page, field, "Yes")
        assert page.is_checked("#terms")

    def test_radio_selects_the_matching_option(self, page, fields):
        from jobbot.appliers.forms import fill_field

        field = next(f for f in fields if "relocate" in f.question.question.lower())
        fill_field(page, field, "Yes")
        assert page.is_checked("input[name='relocate'][value='Yes']")

    def test_resume_upload_attaches_the_file(self, page, tmp_path):
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 fake")
        assert upload_resume(page, resume) is True

    def test_upload_reports_failure_when_there_is_no_file_input(self, browser, tmp_path):
        page = browser.new_page()
        page.set_content("<form><input type='text' name='x'></form>")
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"x")
        assert upload_resume(page, resume) is False
        page.close()


class TestScoping:
    def test_root_selector_limits_extraction(self, page):
        outside = read_fields(page, "#nonexistent")
        assert outside == []

    def test_whole_page_extraction_works(self, page):
        assert len(read_fields(page)) > 5
