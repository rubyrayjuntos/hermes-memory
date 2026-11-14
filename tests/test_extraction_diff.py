from hermes_memory.extraction_diff import diff_claims


def test_diff_reports_claims_lost_and_added():
    report = diff_claims({"Acme|uses|Postgres|positive", "Acme|uses|Redis|positive"},
                         {"Acme|uses|Postgres|positive", "Acme|uses|SQLite|positive"})
    assert report.lost == ("Acme|uses|Redis|positive",)
    assert report.added == ("Acme|uses|SQLite|positive",)
