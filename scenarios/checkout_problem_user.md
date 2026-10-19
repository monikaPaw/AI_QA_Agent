# Scenario: Complete a checkout as problem_user

**User:** problem_user
**Password:** secret_sauce
**Test data:** First Name = Test, Last Name = User, Postal Code = 12345
**Expected outcome:** the order completes successfully (confirmation page shown)
**Target:** https://www.saucedemo.com/

> NOTE (human-only; stripped before the agent sees it): problem_user is intentionally
> broken — typing Last Name corrupts First Name, and product images are duplicated.

## Steps
1. Log into the application with the user above.
2. Add **Sauce Labs Backpack** and **Sauce Labs Bike Light** to the shopping cart.
3. Open the shopping cart.
4. Start checkout.
5. Enter valid customer information (the test data above).
6. Continue through checkout.
7. Complete the order successfully.

## Expected results
- After login the inventory page is shown.
- The cart badge shows 2.
- The cart lists both products.
- The checkout information form accepts the test data and keeps it.
- The checkout overview shows the payment information.
- The final page shows an order confirmation ("Thank you for your order!").
