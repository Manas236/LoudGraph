import numpy as np

from pipeline.labels import choose_flash_model, parse_json_text, turning_point, validate

YEARS = list(range(1990, 2024))
T = np.arange(len(YEARS))


def test_accepts_good_label():
    assert validate({"label": "Oil price crash", "event_year": 2014, "confident": True}, 2015) == ("Oil price crash", "ok")
    assert validate({"label": "2011 revolution", "event_year": 2011, "confident": True}, 2011)[0] == "2011 revolution"


def test_rejections():
    cases = [
        ({"label": "Oil price crash", "event_year": 2014, "confident": False}, "not confident"),
        ({"label": "Oil price crash", "event_year": 2012, "confident": True}, "not within"),
        ({"label": "The big long national strike", "event_year": 2015, "confident": True}, "words"),
        ({"label": "Hyperinflationaryyyyyyyyy crisis", "event_year": 2015, "confident": True}, "chars"),
        ({"label": "Prices up 80", "event_year": 2015, "confident": True}, "number"),
        ({"label": "1998 crisis", "event_year": 2015, "confident": True}, "number (1998)"),
        ({"label": "Inflation hits 50%", "event_year": 2015, "confident": True}, "number"),
        ({"label": "", "event_year": 2015, "confident": True}, "no label"),
        ({"event_year": 2015, "confident": True}, "no label"),
        ({"label": "War", "event_year": "soon", "confident": True}, "integer"),
        ({"label": "War", "event_year": 2015, "confident": "yes"}, "not confident"),
        (None, "JSON"),
    ]
    for resp, why in cases:
        label, reason = validate(resp, 2015)
        assert label is None and why in reason, (resp, reason)


def test_year_digits_must_be_near_turning_point():
    assert validate({"label": "2016 coup attempt", "event_year": 2016, "confident": True}, 2015)[0]
    assert validate({"label": "2019 crisis", "event_year": 2016, "confident": True}, 2015)[0] is None


def test_parse_json_text():
    assert parse_json_text('```json\n{"label": "x", "event_year": 2000, "confident": true}\n```')["label"] == "x"
    assert parse_json_text('Sure: {"label": "x", "event_year": 2000, "confident": true}')["confident"] is True
    assert parse_json_text("nope") is None


def test_turning_point_crash_uses_largest_move():
    v = 50 + 0.6 * T
    v[20:] -= 25
    tp = turning_point([[y, float(x)] for y, x in zip(YEARS, v)], 1990)
    assert tp["kind"] == "shock" and tp["year"] == YEARS[20]


def test_turning_point_v_uses_trough():
    v = np.abs(T - 17) * 1.5 + 20
    tp = turning_point([[y, float(x)] for y, x in zip(YEARS, v)], 1990)
    assert tp["kind"] == "trough" and tp["year"] == YEARS[17]


def test_choose_flash_model():
    models = [
        {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash-lite", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/gemini-2.5-flash-preview-tts", "supportedGenerationMethods": ["generateContent"]},
        {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
    ]
    assert choose_flash_model(models) == "gemini-2.5-flash"
    assert choose_flash_model([{"name": "models/x-pro", "supportedGenerationMethods": ["generateContent"]}]) is None
