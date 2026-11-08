from tradequery.settings import load_settings


def test_default_settings_load():
    settings = load_settings()
    assert settings.clock.kind == "ny_close"
    assert settings.local_timezone == "Asia/Damascus"
    assert settings.break_even_tolerance == 0.0
    assert settings.sessions == {"asia": (0, 8), "london": (7, 16), "new_york": (12, 21)}


def test_custom_settings_file(tmp_path):
    path = tmp_path / "settings.toml"
    path.write_text(
        '[time]\nserver_clock = "fixed"\nfixed_offset_hours = 3\nlocal_timezone = "UTC"\n'
        "[metrics]\nbreak_even_tolerance = 0.5\n"
    )
    settings = load_settings(path)
    assert settings.clock.kind == "fixed"
    assert settings.clock.fixed_offset_hours == 3.0
    assert settings.break_even_tolerance == 0.5
    assert settings.sessions == {}
