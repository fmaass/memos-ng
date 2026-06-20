from app.config import Settings


def test_is_production_true_when_env_not_development():
    # Gates docs_url/redoc_url exposure in app.main — must be True outside dev.
    assert Settings(ENV="production").is_production is True
    assert Settings(ENV="staging").is_production is True


def test_is_production_false_in_development():
    assert Settings(ENV="development").is_production is False
