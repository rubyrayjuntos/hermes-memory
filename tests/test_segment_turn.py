from hermes_memory.turn_filter import Segment, segment_turn


def test_ordinary_authored_assertion_is_one_authored_segment():
    text = "Nightingale uses Postgres."
    segs = segment_turn(text)
    assert segs == [Segment(0, len(text), "authored")]


def test_fenced_code_is_kind_code():
    text = "```\nConfig uses Redis\n```"
    segs = segment_turn(text)
    assert segs == [Segment(0, len(text), "code")]


def test_quoted_run_is_kind_quote():
    text = "> Bob is the owner.\n> leftover"
    segs = segment_turn(text)
    assert segs == [Segment(0, len(text), "quote")]


PASTED_EMAIL = (
    "From: alice@example.com\n"
    "To: bob@example.com\n"
    "Subject: ownership\n"
    "Date: Tue, 1 Jan 2026 00:00:00 +0000\n"
    "\n"
    "Bob is the owner.\n"
    "> On Mon, Bob wrote:\n"
    "> please confirm\n"
    "\n"
    "-- \n"
    "Sent from my iPhone\n"
    "No, I meant Friday.\n"
)


def test_email_headers_start_a_pasted_segment_to_end():
    segs = segment_turn(PASTED_EMAIL)
    assert segs == [Segment(0, len(PASTED_EMAIL), "pasted")]
    assert "Bob is the owner." in PASTED_EMAIL[segs[0].start:segs[0].end]


def test_mixed_authored_prefix_then_pasted_email():
    prefix = "Nightingale uses Postgres. Can you clean this up?\n\n"
    text = prefix + PASTED_EMAIL
    segs = segment_turn(text)
    assert [s.kind for s in segs] == ["authored", "pasted"]
    assert text[segs[0].start:segs[0].end] == prefix
    assert text[segs[1].start:segs[1].end] == PASTED_EMAIL


def test_log_run_is_pasted():
    text = "\n".join(f"2026-01-01 00:{i:02d}:00 INFO Worker uses 4GB" for i in range(60))
    segs = segment_turn(text)
    assert segs == [Segment(0, len(text), "pasted")]


def test_absence_of_first_person_does_not_make_an_authored_assertion_a_paste():
    text = "Nightingale uses Postgres for the ledger."
    segs = segment_turn(text)
    assert segs == [Segment(0, len(text), "authored")]


def test_markdown_table_is_pasted():
    text = "| Name | Role |\n| Bob | owner |\n| Ann | editor |"
    segs = segment_turn(text)
    assert segs == [Segment(0, len(text), "pasted")]


def test_signature_delimiter_starts_pasted_tail():
    text = "Nightingale uses Postgres.\n-- \nSent from my phone"
    segs = segment_turn(text)
    assert [s.kind for s in segs] == ["authored", "pasted"]
    assert text[segs[0].start:segs[0].end] == "Nightingale uses Postgres.\n"
