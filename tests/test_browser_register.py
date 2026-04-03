from src.core.browser_register import BrowserRegistrationEngine


def _build_engine():
    engine = BrowserRegistrationEngine.__new__(BrowserRegistrationEngine)
    engine.logs = []
    engine.callback_logger = lambda _msg: None
    engine.task_uuid = None
    return engine


class FakeBirthdateLocator:
    def __init__(self):
        self.fills = []
        self._value = ""

    def fill(self, value):
        self.fills.append(value)
        if len(self.fills) == 1:
            self._value = "04/04/2026"
        else:
            self._value = value

    def input_value(self):
        return self._value


class FakeActionLocator:
    def __init__(self, page, label):
        self.page = page
        self.label = label

    @property
    def first(self):
        return self

    def wait_for(self, state="visible", timeout=0):
        if self.label not in self.page.visible_labels:
            raise RuntimeError(f"{self.label} not visible")

    def click(self):
        self.page.clicked.append(self.label)
        next_url = self.page.transitions.get(self.label)
        if next_url:
            self.page.url = next_url


class FakePage:
    def __init__(self, url, visible_labels, transitions):
        self.url = url
        self.visible_labels = set(visible_labels)
        self.transitions = dict(transitions)
        self.clicked = []

    def locator(self, selector):
        label = selector.split('has-text("', 1)[1].rsplit('")', 1)[0]
        return FakeActionLocator(self, label)

    def wait_for_timeout(self, _ms):
        return None


def test_extract_callback_candidate_supports_nested_login_web_callback():
    nested = (
        "https://chatgpt.com/api/auth/callback/login-web"
        "?callbackUrl=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback%3Fcode%3Dabc%26state%3Dxyz"
    )

    assert (
        BrowserRegistrationEngine._extract_callback_candidate(nested)
        == "http://localhost:1455/auth/callback?code=abc&state=xyz"
    )


def test_repair_browser_birthdate_value_rewrites_mdy_year_into_safe_range():
    repaired = BrowserRegistrationEngine._repair_browser_birthdate_value("04/04/2026", "2005-04-04")

    assert repaired == "04/04/2000"


def test_repair_browser_birthdate_value_rewrites_ymd_year_into_safe_range():
    repaired = BrowserRegistrationEngine._repair_browser_birthdate_value("2026/04/04", "1998-04-04")

    assert repaired == "1998/04/04"


def test_fill_birthdate_field_repairs_browser_misread_year():
    engine = _build_engine()
    locator = FakeBirthdateLocator()

    engine._fill_birthdate_field(locator, "2005-04-04")

    assert locator.fills == ["2000-04-04", "04/04/2000"]


def test_step_post_signup_clicks_login_and_returns_nested_callback():
    engine = _build_engine()
    nested = (
        "https://chatgpt.com/api/auth/callback/login-web"
        "?callbackUrl=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback%3Fcode%3Dabc%26state%3Dxyz"
    )
    page = FakePage(
        url="https://chatgpt.com/log-in-or-create-account",
        visible_labels={"Log in"},
        transitions={"Log in": nested},
    )

    callback_url = engine._step_post_signup(page)

    assert page.clicked == ["Log in"]
    assert callback_url == "http://localhost:1455/auth/callback?code=abc&state=xyz"
