"""Deterministic two-version shop used only for local QualityProof demonstrations."""

from __future__ import annotations

import argparse
import html
import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal, TypedDict, cast
from urllib.parse import parse_qs

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

DemoVersion = Literal["v1", "v2"]
DEMO_NOTICE = "LOCAL DEMO ONLY — credentials and sessions are intentionally non-production."


class UserRecord(TypedDict):
    password: str
    role: str
    name: str


class ProductRecord(TypedDict):
    name: str
    price: Decimal


USERS: dict[str, UserRecord] = {
    "shopper@example.test": {"password": "shopper-demo", "role": "normal", "name": "Sam Shopper"},
    "admin@example.test": {"password": "admin-demo", "role": "admin", "name": "Ada Admin"},
}
PRODUCTS: dict[int, ProductRecord] = {
    1: {"name": "Accessible Mug", "price": Decimal("12.50")},
    2: {"name": "Traceable Tote", "price": Decimal("8.25")},
    3: {"name": "Deterministic Notebook", "price": Decimal("5.00")},
}


@dataclass
class DemoState:
    """Small resettable state boundary; no data leaves the process."""

    carts: dict[str, dict[int, int]] = field(default_factory=dict)
    profiles: dict[str, dict[str, str]] = field(default_factory=dict)

    def reset(self) -> None:
        self.carts.clear()
        self.profiles.clear()


def _styles(version: DemoVersion) -> str:
    overflow = (
        ".seed-overflow{width:1200px;border:2px dashed #b42318;padding:.5rem}"
        if version == "v2"
        else ""
    )
    return f"""
*{{box-sizing:border-box}} body{{font:16px/1.5 system-ui,sans-serif;margin:0;color:#17202a}}
header,main,footer{{max-width:70rem;margin:auto;padding:1rem}} header{{background:#eef4ff}}
nav a{{margin-right:1rem}} .notice{{background:#fff4ce;border-left:4px solid #9a6700;padding:.8rem}}
.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(14rem,1fr));gap:1rem}}
.card{{border:1px solid #ccd5df;border-radius:.4rem;padding:1rem}}
label{{display:block;margin-top:.6rem}}
input{{font:inherit;max-width:100%;padding:.4rem}}
button,.button{{font:inherit;padding:.5rem .8rem}}
.error{{color:#b42318}} .price{{font-weight:700}} table{{border-collapse:collapse;width:100%}}
th,td{{border-bottom:1px solid #ccd5df;padding:.5rem;text-align:left}} {overflow}
@media(max-width:35rem){{nav a{{display:block;margin:.25rem 0}}}}
"""


def _layout(version: DemoVersion, title: str, body: str, user: str | None = None) -> str:
    account = USERS.get(user) if user is not None else None
    role = account["role"] if account is not None else None
    admin_link = '<a href="/admin">Admin</a>' if user else ""
    session_link = (
        '<form method="post" action="/logout"><button type="submit">Log out</button></form>'
        if user
        else '<a href="/login">Log in</a>'
    )
    identity = f"<p>Signed in as {html.escape(user or '')} ({role})</p>" if user else ""
    help_path = "help" if version == "v1" else "missing-help"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)} · QualityProof Demo</title><style>{_styles(version)}</style></head>
<body data-demo-version="{version}"><header><strong>QualityProof Seed Shop ({version})</strong>
<nav aria-label="Primary"><a href="/products">Products</a><a href="/cart">Cart</a>
<a href="/profile">Profile</a>{admin_link}<a href="/captcha-demo">Human-check demo</a>
{session_link}</nav>{identity}</header><main>
<p class="notice">{DEMO_NOTICE}</p>{body}</main>
<footer><a href="/{help_path}">Help</a></footer></body></html>"""


def _user(request: Request) -> str | None:
    value = request.cookies.get("demo_user")
    return value if value in USERS else None


async def _form(request: Request) -> dict[str, str]:
    parsed = parse_qs((await request.body()).decode("utf-8"), keep_blank_values=True)
    return {key: values[-1] for key, values in parsed.items()}


def _html(version: DemoVersion, title: str, body: str, user: str | None = None) -> HTMLResponse:
    return HTMLResponse(_layout(version, title, body, user))


def _require_login(request: Request) -> str | RedirectResponse:
    return _user(request) or RedirectResponse("/login", status_code=303)


def _money(value: Decimal) -> str:
    return f"{value.quantize(Decimal('0.01')):.2f}"


def create_app(version: DemoVersion = "v1") -> FastAPI:
    """Create one isolated, deterministic seeded version of the demo."""
    if version not in {"v1", "v2"}:
        raise ValueError("demo version must be v1 or v2")
    app = FastAPI(title=f"QualityProof Seed Shop {version}")
    state = DemoState()
    app.state.demo_state = state
    app.state.demo_version = version

    @app.get("/", include_in_schema=False)
    async def index() -> RedirectResponse:
        return RedirectResponse("/products", status_code=303)

    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request) -> HTMLResponse:
        body = """<h1>Demo login</h1>
<p>Safe demo accounts are listed in <code>demo/README.md</code>.</p>
<form method="post"><label for="email">Email</label><input id="email" name="email" type="email">
<label for="password">Password</label><input id="password" name="password" type="password">
<button type="submit">Log in</button></form>"""
        return _html(version, "Login", body, _user(request))

    @app.post("/login")
    async def login(request: Request) -> Response:
        form = await _form(request)
        account = USERS.get(form.get("email", ""))
        if account is None or account["password"] != form.get("password"):
            return _html(version, "Login", '<h1>Demo login</h1><p class="error">Invalid login.</p>')
        response = RedirectResponse("/products", status_code=303)
        response.set_cookie("demo_user", form["email"], httponly=True, samesite="strict")
        return response

    @app.post("/logout")
    async def logout() -> RedirectResponse:
        response = RedirectResponse("/products", status_code=303)
        response.delete_cookie("demo_user")
        return response

    @app.get("/products", response_class=HTMLResponse)
    async def products(request: Request) -> HTMLResponse:
        cards = "".join(
            f'<article class="card"><h2><a href="/products/{product_id}">{item["name"]}</a></h2>'
            f'<p class="price">£{_money(item["price"])}</p></article>'
            for product_id, item in PRODUCTS.items()
        )
        legacy = '<p><a href="/legacy-order">Legacy quick order</a></p>' if version == "v1" else ""
        overflow = (
            '<p class="seed-overflow" data-seed-defect="layout-overflow">Seeded overflow marker</p>'
            if version == "v2"
            else ""
        )
        return _html(
            version,
            "Products",
            f'<h1>Products</h1><div class="grid">{cards}</div>{legacy}{overflow}',
            _user(request),
        )

    @app.get("/products/{product_id}", response_class=HTMLResponse)
    async def product(request: Request, product_id: int) -> HTMLResponse:
        item = PRODUCTS.get(product_id)
        if item is None:
            return _html(version, "Not found", "<h1>Product not found</h1>", _user(request))
        locator = (
            'id="add-to-cart" data-testid="add-product"'
            if version == "v1"
            else 'id="basket-add" data-testid="basket-product"'
        )
        body = (
            f'<h1>{item["name"]}</h1><p class="price">£{_money(item["price"])}</p>'
            f'<form method="post" action="/cart/add"><input type="hidden" name="product_id" '
            f'value="{product_id}"><label for="quantity">Quantity</label>'
            f'<input id="quantity" name="quantity" type="number" min="1" value="1">'
            f'<button {locator} type="submit">Add to cart</button></form>'
        )
        return _html(version, str(item["name"]), body, _user(request))

    @app.post("/cart/add")
    async def add_to_cart(request: Request) -> RedirectResponse:
        user = _user(request) or "guest"
        form = await _form(request)
        product_id = int(form.get("product_id", "0"))
        quantity = max(1, int(form.get("quantity", "1")))
        if product_id in PRODUCTS:
            cart = state.carts.setdefault(user, {})
            cart[product_id] = cart.get(product_id, 0) + quantity
        return RedirectResponse("/cart", status_code=303)

    @app.get("/cart", response_class=HTMLResponse)
    async def cart(request: Request) -> HTMLResponse:
        user = _user(request) or "guest"
        rows = "".join(
            f"<tr><td>{PRODUCTS[product_id]['name']}</td><td>{quantity}</td></tr>"
            for product_id, quantity in sorted(state.carts.get(user, {}).items())
        )
        return _html(
            version,
            "Cart",
            f"<h1>Cart</h1><table><tr><th>Product</th><th>Quantity</th></tr>{rows}</table>"
            '<p><a href="/checkout">Review checkout</a></p>',
            _user(request),
        )

    @app.get("/checkout", response_class=HTMLResponse)
    async def checkout(request: Request) -> Response:
        required = _require_login(request)
        if isinstance(required, RedirectResponse):
            return required
        cart_items = state.carts.get(required, {})
        subtotal = sum(
            (
                PRODUCTS[product_id]["price"] * (quantity if version == "v1" else 1)
                for product_id, quantity in cart_items.items()
            ),
            Decimal(),
        )
        shipping = Decimal("3.00") if cart_items else Decimal()
        total = subtotal + shipping
        return _html(
            version,
            "Checkout",
            "<h1>Checkout</h1><dl><dt>Subtotal</dt>"
            f'<dd data-testid="subtotal">£{_money(subtotal)}</dd>'
            f"<dt>Shipping</dt><dd>£{_money(shipping)}</dd><dt>Total</dt>"
            f'<dd data-testid="total">£{_money(total)}</dd></dl>'
            '<a href="/danger/delete-account">Delete account</a>',
            required,
        )

    @app.get("/profile", response_class=HTMLResponse)
    async def profile(request: Request) -> Response:
        required = _require_login(request)
        if isinstance(required, RedirectResponse):
            return required
        phone_label = '<label for="phone">Phone</label>' if version == "v1" else ""
        body = f"""<h1>Profile</h1><form method="post">
<label for="display_name">Display name</label>
<input id="display_name" name="display_name" value="{USERS[required]["name"]}">
<label for="contact_email">Contact email</label><input id="contact_email" name="contact_email"
value="{required}">{phone_label}<input id="phone" name="phone">
<button>Save profile</button></form>"""
        return _html(version, "Profile", body, required)

    @app.post("/profile", response_class=HTMLResponse)
    async def save_profile(request: Request) -> Response:
        required = _require_login(request)
        if isinstance(required, RedirectResponse):
            return required
        form = await _form(request)
        email = form.get("contact_email", "")
        if version == "v1" and ("@" not in email or email.endswith("@")):
            return _html(
                version,
                "Profile",
                '<h1>Profile</h1><p class="error" role="alert">Enter a valid contact email.</p>',
                required,
            )
        state.profiles[required] = dict(form)
        return _html(version, "Profile", "<h1>Profile</h1><p>Profile saved.</p>", required)

    @app.get("/admin", response_class=HTMLResponse)
    async def admin(request: Request) -> Response:
        required = _require_login(request)
        if isinstance(required, RedirectResponse):
            return required
        role = USERS[required]["role"]
        if version == "v1" and role != "admin":
            return HTMLResponse(_layout(version, "Forbidden", "<h1>Forbidden</h1>", required), 403)
        return _html(version, "Admin", "<h1>Admin dashboard</h1><p>Demo orders: 3</p>", required)

    @app.get("/help", response_class=HTMLResponse)
    async def help_page(request: Request) -> HTMLResponse:
        return _html(
            version, "Help", "<h1>Help</h1><p>This is local deterministic help.</p>", _user(request)
        )

    @app.get("/captcha-demo", response_class=HTMLResponse)
    async def captcha_demo(request: Request) -> HTMLResponse:
        body = """<h1>CAPTCHA test-mode demonstration</h1><div class="captcha">
<p>This is a first-party static test challenge, not a CAPTCHA bypass.</p>
<label for="challenge">Type DEMO</label><input id="challenge" value=""></div>"""
        return _html(version, "CAPTCHA demo", body, _user(request))

    if version == "v1":

        @app.get("/legacy-order", response_class=HTMLResponse)
        async def legacy_order(request: Request) -> HTMLResponse:
            return _html(
                version,
                "Legacy order",
                "<h1>Legacy quick order</h1><p>This journey is removed in v2.</p>",
                _user(request),
            )

    @app.get("/danger/delete-account", response_class=HTMLResponse)
    async def destructive_page() -> HTMLResponse:
        return HTMLResponse("<h1>Destructive action was not executed</h1>", status_code=405)

    @app.post("/__demo/reset")
    async def reset(request: Request) -> Mapping[str, str]:
        if request.client is None or request.client.host not in {"127.0.0.1", "testclient"}:
            return {"status": "refused"}
        state.reset()
        return {"status": "reset", "version": version}

    return app


_configured_version = os.environ.get("QUALITYPROOF_DEMO_VERSION", "v1")
if _configured_version not in {"v1", "v2"}:
    raise ValueError("QUALITYPROOF_DEMO_VERSION must be v1 or v2")
app = create_app(cast(DemoVersion, _configured_version))


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local QualityProof seeded demo")
    parser.add_argument("--version", choices=("v1", "v2"), default="v1")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(create_app(args.version), host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
