"""Regression tests: the signup half of auth must exist in the UI, not only the API.

Same premise as `test_dashboard_renders_api_fields.py`, applied to auth — **when a
fix adds a capability, follow it to the surface a human reads and assert it
there.** `POST /v1/auth/signup` is reachable by curl the moment it is written; it
is reachable by a *user* only once `dashboard/` grows a form, a handler, a mode
switch and a header that reflects the session. Every one of those is a separate
hop, and a coarse "is it referenced at all" check is exactly the class of bug that
keeps recurring here: a route nothing calls, or a handler nothing binds.

Comment lines are stripped before scanning, for the reason that file documents:
this codebase comments heavily and cites the very identifiers under test, so
scanning raw source would let a dashboard that had stopped calling signup entirely
still pass.
"""

import pathlib
import sys

sys.path.insert(0, '/home/bk/code/defense/src/defense')
sys.path.insert(0, '/home/bk/code/defense/src')

import pytest

_REPO = pathlib.Path('/home/bk/code/defense')
_APP_JS = (_REPO / 'dashboard_legacy/app.js').read_text(encoding='utf-8')
_INDEX = (_REPO / 'dashboard_legacy/index.html').read_text(encoding='utf-8')
_CSS = (_REPO / 'dashboard_legacy/styles.css').read_text(encoding='utf-8')


def _code_only(src: str) -> str:
    """`src` with comment lines removed — a mention is not a use."""
    out = []
    for line in src.splitlines():
        stripped = line.strip()
        if stripped.startswith(('//', '*', '/*', '*/')):
            continue
        out.append(line)
    return "\n".join(out)


_APP_CODE = _code_only(_APP_JS)


# ---------------------------------------------------------------------------
# The endpoints must actually be called
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/v1/auth/signup",   # register
        "/v1/auth/config",   # what the login screen may offer
        "/v1/auth/token",    # log in
        "/v1/auth/me",       # distinguish "no token" from "expired token"
    ],
)
def test_dashboard_calls_the_auth_endpoint(path):
    assert path in _APP_CODE, f"dashboard never calls {path}"


def test_signup_sends_only_username_and_password():
    """No tenant or role in the body — the server assigns both.

    A UI field for either would be a lie about who decides, and would invite a
    future 'why is my tenant ignored?' fix in the wrong layer.
    """
    start = _APP_CODE.index("async function signup(")
    body = _APP_CODE[start:start + 700]
    assert "'/v1/auth/signup'" in body
    assert "tenant_id" not in body
    assert "role:" not in body


# ---------------------------------------------------------------------------
# The form, the handler, and the binding between them
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "element_id",
    [
        "signup-form",
        "signup-username",
        "signup-password",
        "signup-password2",     # confirm — the one check only the client can make
        "signup-error",
        "signup-submit-btn",
        "auth-mode-signin",
        "auth-mode-signup",
        "header-logout-btn",
    ],
)
def test_signup_markup_exists(element_id):
    assert f'id="{element_id}"' in _INDEX, f"#{element_id} missing from index.html"


@pytest.mark.parametrize(
    "element_id",
    ["signup-form", "signup-username", "signup-password", "signup-password2",
     "signup-error", "auth-mode-signup", "header-logout-btn"],
)
def test_signup_markup_is_read_by_the_script(element_id):
    """Markup nobody queries is decoration; a handler nobody binds is dead code."""
    assert f"'{element_id}'" in _APP_CODE, f"app.js never touches #{element_id}"


def test_submit_and_click_handlers_are_bound():
    for fragment in (
        "signupForm.addEventListener('submit', handleSignupSubmit)",
        "logoutBtn.addEventListener('click', logout)",
    ):
        assert fragment in _APP_CODE, f"missing binding: {fragment}"


def test_mode_tabs_are_bound_to_setAuthMode():
    assert "setAuthMode(mode)" in _APP_CODE
    assert "auth-mode-" in _APP_CODE


# ---------------------------------------------------------------------------
# Session lifecycle
# ---------------------------------------------------------------------------


def test_signup_stores_the_token_it_is_given():
    """Signup returns a token so the password need not cross the wire twice."""
    start = _APP_CODE.index("async function signup(")
    body = _APP_CODE[start:start + 700]
    assert "localStorage.setItem('auth_token'" in body


def test_logout_clears_both_credentials_and_tears_down_streams():
    """Otherwise the header reads "Not authenticated" while the SSE streams keep
    delivering data on the credential the user just revoked (§6.6 defect 4)."""
    start = _APP_CODE.index("function logout(")
    body = _APP_CODE[start:start + 500]
    assert "localStorage.removeItem('auth_token')" in body
    assert "localStorage.removeItem('api_key')" in body
    assert "disconnectAllStreams()" in body


def test_a_401_clears_the_remembered_username_too():
    """A stale name in the header outlives the session it described otherwise."""
    start = _APP_CODE.index("if (response.status === 401)")
    body = _APP_CODE[start:start + 400]
    assert "localStorage.removeItem('auth_user')" in body


def test_signup_disabled_by_the_server_is_honoured():
    """`signup_enabled: false` must disable the tab, not merely be fetched."""
    assert "signup_enabled" in _APP_CODE
    assert "signupTab.disabled" in _APP_CODE


def test_validation_error_arrays_are_humanised():
    """FastAPI 422s arrive as [{loc, msg}]. Raw JSON in the error box is the whole
    message the user gets on the signup form, where a 422 is most likely."""
    assert "d.msg" in _APP_CODE


# ---------------------------------------------------------------------------
# Styling exists for the new chrome (an unstyled segmented control is a bug)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selector",
    [".auth-mode-tabs", ".auth-mode-tab", ".auth-mode-tab.active",
     ".auth-mode-tab:disabled", ".form-hint", ".auth-note"],
)
def test_new_login_chrome_is_styled(selector):
    assert selector in _CSS, f"{selector} has no styles"
