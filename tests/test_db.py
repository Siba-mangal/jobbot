from jobbot.db import ApplyRoute, AppStatus, make_fingerprint


class TestFingerprint:
    """The fingerprint is the cross-site dedupe key. Same role on two boards
    must collapse to one value; genuinely different roles must not collide."""

    def test_identical_inputs_match(self):
        a = make_fingerprint("Acme Corp", "Backend Engineer", "Bangalore")
        b = make_fingerprint("Acme Corp", "Backend Engineer", "Bangalore")
        assert a == b

    def test_case_and_punctuation_insensitive(self):
        a = make_fingerprint("Acme Corp.", "Backend Engineer", "Bangalore")
        b = make_fingerprint("ACME CORP", "backend engineer", "bangalore")
        assert a == b

    def test_seniority_tokens_ignored(self):
        # LinkedIn says "Senior Backend Engineer", Instahyre says "Backend Engineer".
        a = make_fingerprint("Acme", "Senior Backend Engineer", "Bangalore")
        b = make_fingerprint("Acme", "Backend Engineer", "Bangalore")
        assert a == b

    def test_location_reduced_to_first_component(self):
        a = make_fingerprint("Acme", "Backend Engineer", "Bangalore, KA, India")
        b = make_fingerprint("Acme", "Backend Engineer", "Bangalore")
        assert a == b

    def test_different_company_differs(self):
        a = make_fingerprint("Acme", "Backend Engineer", "Bangalore")
        b = make_fingerprint("Globex", "Backend Engineer", "Bangalore")
        assert a != b

    def test_different_role_differs(self):
        a = make_fingerprint("Acme", "Backend Engineer", "Bangalore")
        b = make_fingerprint("Acme", "Data Scientist", "Bangalore")
        assert a != b

    def test_different_city_differs(self):
        a = make_fingerprint("Acme", "Backend Engineer", "Bangalore")
        b = make_fingerprint("Acme", "Backend Engineer", "Hyderabad")
        assert a != b

    def test_empty_location_is_stable(self):
        assert make_fingerprint("Acme", "Backend Engineer", "") == make_fingerprint(
            "Acme", "Backend Engineer", ""
        )


class TestApplyRoute:
    def test_only_board_greenhouse_lever_are_automated(self):
        automated = {r for r in ApplyRoute if r.is_automated}
        assert automated == {
            ApplyRoute.BOARD_NATIVE,
            ApplyRoute.ATS_GREENHOUSE,
            ApplyRoute.ATS_LEVER,
        }

    def test_unknown_and_other_route_to_manual(self):
        assert not ApplyRoute.ATS_OTHER.is_automated
        assert not ApplyRoute.UNKNOWN.is_automated


class TestEnums:
    def test_status_values_are_stable_strings(self):
        # These land in the DB and the dashboard URLs; renaming is a migration.
        assert AppStatus.NEW == "new"
        assert AppStatus.NEEDS_INPUT == "needs_input"
        assert AppStatus.SUBMITTED == "submitted"
