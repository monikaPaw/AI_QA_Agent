# AUTO-GENERATED from scenarios/checkout_problem_user.md - do not edit by hand.
# Regenerate: python tools/generate_tests.py --scenario scenarios/checkout_problem_user.md --force
from steps.step_helpers import (
    VALID_FIRST,
    VALID_LAST,
    VALID_ZIP,
    add_items_to_cart,
    continue_checkout,
    fill_customer_info,
    finish_order,
    login,
    open_cart,
    read_customer_info,
    start_checkout,
)


def test_checkout_problem_user(page):
    # 1. Log into the application
    login(page, "problem_user", "secret_sauce")
    assert page.is_visible(".inventory_list"), "Inventory page should be visible after login"

    # 2. Add Sauce Labs Backpack and Sauce Labs Bike Light to the shopping cart
    add_items_to_cart(page)
    badge = page.text_content(".shopping_cart_badge")
    assert badge == "2", f"Cart badge should be 2 after adding both items, got {badge!r}"

    # 3. Open the shopping cart
    open_cart(page)
    items = page.locator(".cart_item")
    assert items.count() == 2, f"Cart should contain 2 items, got {items.count()}"
    assert page.is_visible("text=Sauce Labs Backpack"), "Backpack should be listed in cart"
    assert page.is_visible("text=Sauce Labs Bike Light"), "Bike Light should be listed in cart"

    # 4. Start checkout
    start_checkout(page)
    assert page.is_visible("#first-name"), "Checkout information form should be visible"

    # 5. Enter valid customer information
    fill_customer_info(page)
    values = read_customer_info(page)
    assert values["first_name"] == VALID_FIRST, f"First Name should retain {VALID_FIRST!r}, got {values['first_name']!r}"
    assert values["last_name"] == VALID_LAST, f"Last Name should retain {VALID_LAST!r}, got {values['last_name']!r}"
    assert values["postal_code"] == VALID_ZIP, f"Postal Code should retain {VALID_ZIP!r}, got {values['postal_code']!r}"

    # 6. Continue through checkout
    continue_checkout(page)
    page.wait_for_selector("#finish", timeout=10000)
    assert page.is_visible("text=Payment Information"), "Overview should show payment section"

    # 7. Complete the order successfully
    assert finish_order(page), "Order did not complete: confirmation page never appeared"
    header = page.text_content(".complete-header")
    assert "Thank you" in header, f"Unexpected confirmation header: {header!r}"
    assert page.is_visible("text=Your order has been dispatched"), "Dispatch confirmation text missing"
