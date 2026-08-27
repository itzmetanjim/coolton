from agent import provider_config


def test_get_all_tags_matches_providers_json():
    # These three are the ones configured today (luna, glm5.2, glm5.3-flash) —
    # update this if providers.json's tags are intentionally changed.
    assert provider_config.get_all_tags() == ["glm5.2", "glm5.3-flash", "luna"]


def test_extract_tag_directive_no_directive_is_unchanged():
    text, tag, error = provider_config.extract_tag_directive("just a normal message")
    assert text == "just a normal message"
    assert tag is None
    assert error is None


def test_extract_tag_directive_strips_valid_directive():
    text, tag, error = provider_config.extract_tag_directive("hello [!WITH:luna] world")
    assert text == "hello  world"
    assert tag == "luna"
    assert error is None


def test_extract_tag_directive_is_case_insensitive():
    text, tag, error = provider_config.extract_tag_directive("[!WITH:LUNA] hi")
    assert tag == "luna"
    assert error is None


def test_extract_tag_directive_strips_surrounding_whitespace_in_tag():
    text, tag, error = provider_config.extract_tag_directive("[!WITH: luna ] hi")
    assert tag == "luna"
    assert error is None


def test_extract_tag_directive_escaped_strips_only_backslash():
    text, tag, error = provider_config.extract_tag_directive(r"hello \[!WITH:luna] world")
    assert text == "hello [!WITH:luna] world"
    assert tag is None
    assert error is None


def test_extract_tag_directive_unknown_tag_returns_error():
    text, tag, error = provider_config.extract_tag_directive("[!WITH:bogus] hi")
    assert tag is None
    assert error is not None
    assert "bogus" in error
    assert "luna" in error and "glm5.2" in error and "glm5.3-flash" in error
    assert r"\[!WITH:bogus]" in error


def test_extract_tag_directive_escaped_unknown_tag_is_not_an_error():
    text, tag, error = provider_config.extract_tag_directive(r"\[!WITH:bogus] hi")
    assert text == "[!WITH:bogus] hi"
    assert tag is None
    assert error is None


def test_extract_tag_directive_only_first_live_directive_wins():
    text, tag, error = provider_config.extract_tag_directive("[!WITH:luna] and also [!WITH:glm5.2]")
    assert tag == "luna"
    assert error is None
    assert "[!WITH:" not in text
