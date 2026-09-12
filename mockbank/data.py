"""Seeded fixture data. Entirely fabricated -- no real people, no real PII.

The SSN field exists for one reason: to give the redaction layer something real
to redact in observations, prompts, screenshots, and evidence.
"""

from dataclasses import dataclass, field


@dataclass
class Account:
    number: str
    kind: str  # "Savings" | "Checking" | "Certificate"
    balance_cents: int
    status: str = "Open"

    @property
    def balance(self) -> str:
        return f"${self.balance_cents / 100:,.2f}"


@dataclass
class Member:
    member_id: str
    first_name: str
    last_name: str
    ssn: str  # fabricated
    date_of_birth: str
    branch: str
    status: str
    accounts: list[Account] = field(default_factory=list)
    restricted: bool = False  # drives the permission-denied path deterministically

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}"

    def account_of(self, kind: str) -> Account | None:
        return next((a for a in self.accounts if a.kind.lower() == kind.lower()), None)


MEMBERS: dict[str, Member] = {
    "12345": Member(
        member_id="12345",
        first_name="Dana",
        last_name="Whitfield",
        ssn="521-84-9077",
        date_of_birth="1979-03-11",
        branch="Riverside",
        status="Active",
        accounts=[
            Account("100045218", "Savings", 421075),
            Account("100045219", "Checking", 81230),
        ],
    ),
    "23456": Member(
        member_id="23456",
        first_name="Marcus",
        last_name="Ellery",
        ssn="404-27-5510",
        date_of_birth="1988-11-02",
        branch="Northgate",
        status="Active",
        accounts=[Account("100077431", "Savings", 1290055)],
    ),
    "34567": Member(
        member_id="34567",
        first_name="Priya",
        last_name="Nandakumar",
        ssn="613-55-2048",
        date_of_birth="1994-07-24",
        branch="Riverside",
        status="Active",
        accounts=[
            Account("100091002", "Savings", 75000),
            Account("100091003", "Certificate", 2500000),
        ],
    ),
    # Exists, but the service account may not view it -> a permission-denied
    # result that is a legitimate business outcome, not a crash.
    "45678": Member(
        member_id="45678",
        first_name="Terrence",
        last_name="Oyelaran",
        ssn="298-10-7734",
        date_of_birth="1965-01-30",
        branch="Executive",
        status="Active",
        restricted=True,
        accounts=[Account("100012001", "Savings", 88000000)],
    ),
}

PRODUCT_CODES = [
    ("SAV", "Regular Savings"),
    ("SAV2", "Premium Savings"),
    ("HSA", "Health Savings"),
    ("VAC", "Vacation Club"),
]

# Sub-accounts opened during a run. In-process only; reset between test runs.
OPENED: list[dict] = []

_NEXT_ACCOUNT_SEQ = [200050000]


def next_account_number() -> str:
    _NEXT_ACCOUNT_SEQ[0] += 1
    return str(_NEXT_ACCOUNT_SEQ[0])


def find_members(query: str) -> list[Member]:
    """Exact match on member id, otherwise case-insensitive surname prefix."""
    q = (query or "").strip()
    if not q:
        return []
    if q in MEMBERS:
        return [MEMBERS[q]]
    ql = q.lower()
    return [m for m in MEMBERS.values() if m.last_name.lower().startswith(ql)]


def reset() -> None:
    OPENED.clear()
    _NEXT_ACCOUNT_SEQ[0] = 200050000
