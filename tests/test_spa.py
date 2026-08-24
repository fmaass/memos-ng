from app.main import FRONTEND_DIR, resolve_spa_file

INDEX = FRONTEND_DIR / "index.html"


def test_parent_traversal_returns_index():
    # `../` escaping the dist dir must never resolve to a real outside file.
    assert resolve_spa_file("../../../../../../etc/passwd") == INDEX


def test_prefix_sibling_dir_is_not_served():
    # Regression for the lexical-startswith bug: a sibling dir sharing the "dist"
    # prefix (frontend/dist-secret) passes a `startswith(dist)` check but is NOT
    # under dist. With a real file present, the old code would have leaked it.
    sibling = FRONTEND_DIR.parent / "dist-secret"
    sibling.mkdir(parents=True, exist_ok=True)
    secret = sibling / "secret.txt"
    secret.write_text("SECRET")
    try:
        result = resolve_spa_file("../dist-secret/secret.txt")
        assert result != secret  # must not serve the sibling file
        assert result == INDEX
    finally:
        secret.unlink(missing_ok=True)
        sibling.rmdir()


def test_unknown_spa_path_returns_index():
    assert resolve_spa_file("explore/some/deep/route") == INDEX


def test_memo_permalink_returns_index():
    # /m/<uid> is the canonical memo URL (and the AASA applinks pattern the app claims). It is a
    # SPA route with no file behind it, so the shell has to answer it — otherwise a cold open or a
    # reload of a shared link 404s while in-app navigation to the same URL works.
    assert resolve_spa_file("m/01J8Z3K9QW7X2E5R6T7Y8U9I0P") == INDEX
