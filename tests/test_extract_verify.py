from sprinto.extract import QBool, QList, QStr, merge, verify_field
from sprinto.models import Evidence, ExtractedFields

PAGES = {
    "https://acme.io/": "Acme is a B2B SaaS platform for mid-market finance teams.\n"
                        "We are working towards SOC 2 Type II and expect our audit this year.",
    "https://acme.io/pricing": "Our Enterprise plan includes SSO, SAML and audit logs.",
}


def test_verbatim_quote_verified():
    ev, why = verify_field("is_saas", QBool(value=True, evidence_quote="a B2B SaaS platform",
                                             source_url="https://acme.io/"), PAGES)
    assert ev.known and why is None


def test_normalised_match_handles_whitespace_case_and_curly_quotes():
    ev, _ = verify_field("is_saas", QBool(value=True, evidence_quote="Acme  is a b2b\nSaaS",
                                          source_url="https://acme.io"), PAGES)
    assert ev.known


def test_paraphrase_rejected_becomes_unknown():
    ev, why = verify_field("is_saas", QBool(value=True, evidence_quote="Acme is a SaaS company for finance",
                                            source_url="https://acme.io/"), PAGES)
    assert not ev.known and ev.value is None and why == "quote_not_found"


def test_wrong_source_url_corrected_to_page_with_quote():
    ev, _ = verify_field("enterprise_tier_present",
                         QBool(value=True, evidence_quote="Enterprise plan includes SSO", source_url="https://acme.io/"),
                         PAGES)
    assert ev.known and ev.source_url == "https://acme.io/pricing"


def test_quote_from_unfetched_page_rejected():
    ev, why = verify_field("is_saas", QBool(value=True, evidence_quote="a B2B SaaS platform",
                                            source_url="https://elsewhere.com/"),
                           {"https://elsewhere-not-fetched.com/": "nothing here at all"})
    assert not ev.known


def test_short_quote_rejected():
    ev, why = verify_field("is_saas", QBool(value=True, evidence_quote="SaaS", source_url="https://acme.io/"), PAGES)
    assert not ev.known and why == "quote_missing_or_too_short"


def test_invented_list_items_dropped():
    ev, _ = verify_field("certifications_named",
                         QList(value=["SOC 2 Type II", "ISO 27001"],
                               evidence_quote="working towards SOC 2 Type II", source_url="https://acme.io/"), PAGES)
    assert ev.value == ["SOC 2 Type II"]


CAREERS = {"https://resend.com/careers":
           "Platform Engineer · Europe / Remote / Full-time · Security Engineer, Platform · Americas / Remote / "
           "Full-time · Security Engineer, Platform · Europe / Remote / Full-time · Software Engineer, Developer "
           "Tools · Europe / Remote / Full-time · Software Engineer, Trust & Safety · Americas / Remote / Full-time"}


def _roles(*titles):
    from sprinto.extract import QRoles, RoleOut
    return [RoleOut(title=t, posted_date=None, url=None) for t in titles]


def test_stitched_list_quote_accepted_only_if_every_piece_is_on_the_page():
    from sprinto.extract import QRoles
    # live case: the model skipped "Software Engineer, Developer Tools" between two items
    stitched = ("Security Engineer, Platform · Americas / Remote / Full-time · "
                "Software Engineer, Trust & Safety · Americas / Remote / Full-time")
    ev, why = verify_field("compliance_security_roles",
                           QRoles(value=_roles("Security Engineer, Platform"), evidence_quote=stitched,
                                  source_url="https://resend.com/careers"), CAREERS)
    assert ev.known and " … " in ev.evidence_quote and "·" not in ev.evidence_quote
    invented = stitched + " · Head of GRC · Remote"
    ev, why = verify_field("compliance_security_roles",
                           QRoles(value=_roles("Security Engineer, Platform"), evidence_quote=invented,
                                  source_url="https://resend.com/careers"), CAREERS)
    assert not ev.known and why == "quote_not_found"


def test_role_titles_must_appear_in_the_quote():
    from sprinto.extract import QRoles
    q = "Security Engineer, Platform · Americas / Remote / Full-time"
    ev, _ = verify_field("compliance_security_roles",
                         QRoles(value=_roles("Security Engineer, Platform", "Chief Information Security Officer"),
                                evidence_quote=q, source_url="https://resend.com/careers"), CAREERS)
    assert [r.title for r in ev.value] == ["Security Engineer, Platform"]


def test_same_role_in_two_locations_is_one_role():
    from sprinto.extract import QRoles
    q = ("Security Engineer, Platform · Americas / Remote / Full-time · Security Engineer, Platform · "
         "Europe / Remote / Full-time")
    ev, _ = verify_field("compliance_security_roles",
                         QRoles(value=_roles("Security Engineer, Platform", "Security Engineer, Platform"),
                                evidence_quote=q, source_url="https://resend.com/careers"), CAREERS)
    assert [r.title for r in ev.value] == ["Security Engineer, Platform"]


def test_single_value_fields_still_need_one_contiguous_quote():
    ev, why = verify_field("is_saas", QBool(value=True, evidence_quote="Platform Engineer · Developer Tools",
                                            source_url="https://resend.com/careers"), CAREERS)
    assert not ev.known and why == "quote_not_found"


def test_separator_echoed_as_control_char_still_verifies():
    # live 2026-11-19: Gemini returned "SHOP MEN \u0015 SHOP WOMEN" for the page text "SHOP MEN · SHOP WOMEN"
    from sprinto.extract import QBuyer
    pages = {"https://allbirds.com/": "New arrivals · SHOP MEN · SHOP WOMEN · Free shipping on orders"}
    ev, why = verify_field("buyer_type", QBuyer(value="b2c", evidence_quote="SHOP MEN \u0015 SHOP WOMEN",
                                                source_url="https://allbirds.com/"), pages)
    assert ev.known and why is None


def test_separator_tolerance_does_not_allow_different_words():
    from sprinto.extract import QBuyer
    pages = {"https://allbirds.com/": "SHOP MEN · SHOP WOMEN"}
    ev, why = verify_field("buyer_type", QBuyer(value="b2c", evidence_quote="SHOP KIDS \u0015 SHOP WOMEN",
                                                source_url="https://allbirds.com/"), pages)
    assert not ev.known and why == "quote_not_found"


def test_missing_is_unknown_not_false():
    ev, why = verify_field("compliance_tool_named", QStr(value=None, evidence_quote=None, source_url=None), PAGES)
    assert ev.value is None and not ev.verified and why is None


def test_merge_never_loses_verified_fact():
    old = ExtractedFields(is_saas=Evidence(value=True, evidence_quote="q q q", source_url="u", verified=True))
    new = ExtractedFields()
    assert merge(old, new).is_saas.known
