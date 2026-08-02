import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import conversation_agent as ca


def test_extracts_my_name_is():
    assert ca._extract_name("My name is Ravi") == "Ravi"


def test_extracts_im_pattern():
    assert ca._extract_name("Hi, I'm Priya") == "Priya"


def test_extracts_i_am_pattern():
    assert ca._extract_name("I am Vishwajeet") == "Vishwajeet"


def test_extracts_this_is_pattern():
    assert ca._extract_name("Hi, this is Karan calling") == "Karan"


def test_extracts_multi_word_name():
    assert ca._extract_name("My name is Priya Sharma") == "Priya Sharma"


def test_does_not_false_positive_on_common_phrasing():
    assert ca._extract_name("I'm looking for a dentist") == ""
    assert ca._extract_name("I am not sure yet") == ""
    assert ca._extract_name("I'm just browsing") == ""


def test_no_match_returns_empty():
    assert ca._extract_name("Book an appointment please") == ""


def test_parse_json_block_handles_clean_json():
    result = ca._parse_json_block('{"answered": true, "reply": "ok"}')
    assert result == {"answered": True, "reply": "ok"}


def test_parse_json_block_handles_code_fences():
    result = ca._parse_json_block('```json\n{"answered": true, "reply": "ok"}\n```')
    assert result == {"answered": True, "reply": "ok"}


def test_parse_json_block_handles_stray_leading_text():
    # This is the exact failure mode that was likely causing the
    # "Sorry, could you say that again?" dead-end fallback in real
    # conversations - a model reply with extra commentary around the
    # JSON instead of pure JSON.
    result = ca._parse_json_block('Sure, here you go:\n{"answered": true, "reply": "ok"}\nHope that helps!')
    assert result == {"answered": True, "reply": "ok"}


def test_parse_json_block_raises_clean_error_when_no_json_present():
    import pytest
    with pytest.raises(ValueError):
        ca._parse_json_block("I'm not sure how to answer that.")
