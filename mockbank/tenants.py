"""Tenant variants of the same vendor product.

Both tenants run "Meridian Core 4.2". They differ only the way real tenants of a
shared vendor product differ: branding, field labels, and one extra interstitial.
That difference is the whole point -- it is what a capability artifact recorded
against one tenant has to survive when replayed against the other.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Tenant:
    slug: str
    display_name: str
    product_version: str
    # Label text differs per tenant. Locators keyed on exact accessible name will
    # break across tenants; pattern and anchor-relative candidates will not.
    member_id_label: str
    member_search_heading: str
    interstitial_title: str
    interstitial_body: str
    accent: str


TENANTS: dict[str, Tenant] = {
    "demo-cu": Tenant(
        slug="demo-cu",
        display_name="Demo Credit Union",
        product_version="4.2",
        member_id_label="Member ID",
        member_search_heading="Member Search",
        interstitial_title="What's New",
        interstitial_body="Statement exports now include pending transactions.",
        accent="#1f4e79",
    ),
    "valley-cu": Tenant(
        slug="valley-cu",
        display_name="Valley Credit Union",
        product_version="4.2",
        member_id_label="Account Holder #",
        member_search_heading="Account Holder Lookup",
        interstitial_title="Terms of Use",
        interstitial_body="Review the updated acceptable-use policy before continuing.",
        accent="#5a2d82",
    ),
}

DEFAULT_TENANT = "demo-cu"


def get_tenant(slug: str) -> Tenant | None:
    return TENANTS.get(slug)
