"""Shared scenario steps for the SauceDemo checkout scenario.

Scenario:
1. Log into the application
2. Add Sauce Labs Backpack and Sauce Labs Bike Light to the shopping cart
3. Open the shopping cart
4. Start checkout
5. Enter valid customer information
6. Continue through checkout
7. Complete the order successfully

Each step records itself in a small in-memory timeline and (when a screenshot
hook is installed by `tests/conftest.py`) saves a screenshot right after that
step, so the report can show every step with its own result and evidence.
"""
BASE_URL = "https://www.saucedemo.com/"
VALID_FIRST = "Test"
VALID_LAST = "User"
VALID_ZIP = "12345"

# Canonical step names - used to show steps that were never reached after a failure.
SCENARIO_STEPS = [
    "Log into the application",
    "Add Backpack and Bike Light to cart",
    "Open the shopping cart",
    "Start checkout",
    "Enter valid customer information",
    "Continue through checkout",
    "Complete the order",
]

# ---------------------------------------------------------------- timeline
_STEPS: list = []
_SCREENSHOTTER = None


def clear_steps() -> None:
    _STEPS.clear()


def get_steps() -> list:
    return list(_STEPS)


def set_screenshotter(fn) -> None:
    """conftest installs a callable(index, name) -> relative screenshot path."""
    global _SCREENSHOTTER
    _SCREENSHOTTER = fn


def _step(name: str, detail: str = "", url: str = "") -> None:
    index = len(_STEPS) + 1
    shot = None
    if _SCREENSHOTTER is not None:
        try:
            shot = _SCREENSHOTTER(index, name)
        except Exception:
            shot = None
    _STEPS.append({"index": index, "name": name, "detail": detail, "url": url, "screenshot": shot})


def login(page, username: str, password: str) -> None:
    """Step 1: log in and wait for the inventory page."""
    page.goto(BASE_URL)
    page.fill("#user-name", username)
    page.fill("#password", password)
    page.click("#login-button")
    page.wait_for_selector(".inventory_list", timeout=15000)
    _step("Log into the application", f"username={username!r}, landed on {page.url}", page.url)


def add_items_to_cart(page) -> None:
    """Step 2: add Sauce Labs Backpack and Sauce Labs Bike Light."""
    page.click("#add-to-cart-sauce-labs-backpack")
    page.click("#add-to-cart-sauce-labs-bike-light")
    page.wait_for_selector(".shopping_cart_badge", timeout=10000)
    badge = page.text_content(".shopping_cart_badge")
    _step("Add Backpack and Bike Light to cart", f"cart badge={badge!r}", page.url)


def open_cart(page) -> None:
    """Step 3: open the shopping cart."""
    page.click(".shopping_cart_link")
    page.wait_for_selector(".cart_list", timeout=10000)
    count = page.locator(".cart_item").count()
    _step("Open the shopping cart", f"cart items={count}", page.url)


def start_checkout(page) -> None:
    """Step 4: start checkout."""
    page.click("#checkout")
    page.wait_for_selector("#first-name", timeout=10000)
    _step("Start checkout", "checkout information form visible", page.url)


def fill_customer_info(page, first=VALID_FIRST, last=VALID_LAST, zip_code=VALID_ZIP) -> None:
    """Step 5: enter valid customer information."""
    page.fill("#first-name", first)
    page.fill("#last-name", last)
    page.fill("#postal-code", zip_code)


def read_customer_info(page) -> dict:
    """Read back the actual field values (needed to detect the problem_user bug)."""
    values = {
        "first_name": page.input_value("#first-name"),
        "last_name": page.input_value("#last-name"),
        "postal_code": page.input_value("#postal-code"),
    }
    _step(
        "Enter valid customer information",
        f"typed first={VALID_FIRST!r} last={VALID_LAST!r} zip={VALID_ZIP!r} | "
        f"actual first={values['first_name']!r} last={values['last_name']!r} zip={values['postal_code']!r}",
        page.url,
    )
    return values


def continue_checkout(page) -> None:
    """Step 6: continue through checkout."""
    page.click("#continue")
    _step("Continue through checkout", "Continue clicked", page.url)


def finish_order(page, timeout=10000) -> bool:
    """Step 7: finish the order. Returns True if the confirmation page appears."""
    page.click("#finish")
    try:
        page.wait_for_selector(".complete-header", timeout=timeout)
        header = page.text_content(".complete-header")
        _step("Complete the order", f"confirmation shown: {header!r}", page.url)
        return True
    except Exception:
        _step("Complete the order", "confirmation page never appeared (order NOT completed)", page.url)
        return False
