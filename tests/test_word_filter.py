from agent.word_filter import filter_bad_words


def test_masks_bad_words():
    out = filter_bad_words("this is some shit with fuck in it")
    assert out == "this is some [redacted] with [redacted] in it"


def test_case_insensitive():
    assert filter_bad_words("SHIT. the Fucking!!") == "[redacted]. the [redacted]!!"


def test_ignores_substrings():
    # benign words containing letters of bad words must be untouched
    assert filter_bad_words("scrap the shipment, ashes") == "scrap the shipment, ashes"


def test_clean_passthrough():
    assert filter_bad_words("all good here") == "all good here"


def test_empty():
    assert filter_bad_words("") == ""
    assert filter_bad_words(None) == None
